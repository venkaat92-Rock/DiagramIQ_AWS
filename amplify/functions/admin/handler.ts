/**
 * The super-admin API.
 *
 * Every route here is refused unless the caller presents a Cognito access
 * token whose `cognito:groups` contains `superadmin`. The check is on the
 * token's claims, verified against the user pool's public keys — not on a list
 * this function keeps, and not on anything the browser sends alongside.
 *
 * Admin actions are themselves written to the audit log. Someone reading the
 * log later should be able to see who was let in, and by whom.
 */
import {
  AdminCreateUserCommand,
  AdminDisableUserCommand,
  AdminEnableUserCommand,
  AdminListGroupsForUserCommand,
  AdminAddUserToGroupCommand,
  AdminRemoveUserFromGroupCommand,
  CognitoIdentityProviderClient,
  ListUsersCommand,
} from '@aws-sdk/client-cognito-identity-provider';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { CognitoJwtVerifier } from 'aws-jwt-verify';

const USER_POOL_ID = process.env.USER_POOL_ID!;
const CLIENT_ID = process.env.USER_POOL_CLIENT_ID!;
const AUDIT_TABLE = process.env.AUDIT_TABLE!;
const RETENTION_DAYS = Number(process.env.AUDIT_RETENTION_DAYS ?? 400);

const cognito = new CognitoIdentityProviderClient({});
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

// The verifier caches the pool's JWKS after the first call, so this is one
// network round trip per cold start rather than one per request.
const verifier = CognitoJwtVerifier.create({
  userPoolId: USER_POOL_ID,
  tokenUse: 'access',
  clientId: CLIENT_ID,
});

const RESPONSE_HEADERS = { 'content-type': 'application/json' };
const reply = (statusCode: number, body: unknown) => ({
  statusCode,
  headers: RESPONSE_HEADERS,
  body: JSON.stringify(body),
});

type Caller = { sub: string; username: string; email: string; groups: string[] };

async function callerFrom(event: any): Promise<Caller> {
  const header: string =
    event?.headers?.authorization ?? event?.headers?.Authorization ?? '';
  const token = header.replace(/^Bearer\s+/i, '').trim();
  if (!token) throw Object.assign(new Error('Sign in to use the admin console.'), { status: 401 });

  let payload: any;
  try {
    payload = await verifier.verify(token);
  } catch {
    // Deliberately vague to the caller, and deliberately specific in the log.
    throw Object.assign(new Error('Your session is not valid. Sign in again.'), { status: 401 });
  }
  const groups: string[] = payload['cognito:groups'] ?? [];
  if (!groups.includes('superadmin')) {
    throw Object.assign(new Error('Super-admin access is required for this action.'), { status: 403 });
  }
  return {
    sub: payload.sub,
    username: payload.username ?? payload.sub,
    email: payload.email ?? '',
    groups,
  };
}

async function audit(caller: Caller, action: string, detail: Record<string, unknown>) {
  const now = new Date();
  try {
    await ddb.send(new PutCommand({
      TableName: AUDIT_TABLE,
      Item: {
        pk: `USER#${caller.sub}`,
        sk: `${now.toISOString()}#${Math.random().toString(36).slice(2, 8)}`,
        day: now.toISOString().slice(0, 10),
        ts: now.toISOString(),
        actor: caller.email || caller.username,
        actorSub: caller.sub,
        action,
        surface: 'admin',
        detail,
        expiresAt: Math.floor(now.getTime() / 1000) + RETENTION_DAYS * 86400,
      },
    }));
  } catch (err) {
    // A failed audit write must not fail the action the user asked for, but it
    // must not vanish either.
    console.error('audit write failed', action, err);
  }
}

const emailOf = (u: any): string =>
  (u.Attributes ?? u.UserAttributes ?? []).find((a: any) => a.Name === 'email')?.Value ?? '';

async function listUsers() {
  const out = await cognito.send(new ListUsersCommand({ UserPoolId: USER_POOL_ID, Limit: 60 }));
  const users = await Promise.all((out.Users ?? []).map(async (u: any) => {
    const groups = await cognito.send(new AdminListGroupsForUserCommand({
      UserPoolId: USER_POOL_ID, Username: u.Username!,
    }));
    return {
      username: u.Username,
      email: emailOf(u),
      enabled: u.Enabled,
      status: u.UserStatus,             // FORCE_CHANGE_PASSWORD until they set one
      created: u.UserCreateDate,
      lastModified: u.UserLastModifiedDate,
      groups: (groups.Groups ?? []).map((g: any) => g.GroupName),
    };
  }));
  users.sort((a: any, b: any) => (a.email || '').localeCompare(b.email || ''));
  return users;
}

async function invite(email: string, superadmin: boolean) {
  const address = String(email || '').trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)) {
    throw Object.assign(new Error(`"${email}" is not an email address.`), { status: 400 });
  }
  await cognito.send(new AdminCreateUserCommand({
    UserPoolId: USER_POOL_ID,
    Username: address,
    UserAttributes: [
      { Name: 'email', Value: address },
      { Name: 'email_verified', Value: 'true' },   // the invite mail proves the address
    ],
    DesiredDeliveryMediums: ['EMAIL'],
  }));
  await cognito.send(new AdminAddUserToGroupCommand({
    UserPoolId: USER_POOL_ID, Username: address, GroupName: superadmin ? 'superadmin' : 'user',
  }));
  return address;
}

async function readLog(limit: number, sub?: string) {
  if (sub) {
    const out = await ddb.send(new QueryCommand({
      TableName: AUDIT_TABLE,
      KeyConditionExpression: 'pk = :pk',
      ExpressionAttributeValues: { ':pk': `USER#${sub}` },
      ScanIndexForward: false,
      Limit: limit,
    }));
    return out.Items ?? [];
  }
  // Newest first across everyone, by day — the table's second index exists so
  // this does not become a scan as the log grows.
  const days = [...Array(7)].map((_, i) => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - i);
    return d.toISOString().slice(0, 10);
  });
  const pages = await Promise.all(days.map((day: string) => ddb.send(new QueryCommand({
    TableName: AUDIT_TABLE,
    IndexName: 'byDay',
    KeyConditionExpression: '#d = :d',
    ExpressionAttributeNames: { '#d': 'day' },
    ExpressionAttributeValues: { ':d': day },
    ScanIndexForward: false,
    Limit: limit,
  })).catch(() => ({ Items: [] }))));
  return pages.flatMap((p: any) => p.Items ?? [])
    .sort((a: any, b: any) => String(b.ts).localeCompare(String(a.ts)))
    .slice(0, limit);
}

export const handler = async (event: any) => {
  const method = event?.requestContext?.http?.method ?? 'POST';
  if (method === 'OPTIONS') return reply(200, {});

  const raw: string = event?.rawPath ?? event?.path ?? '';
  const route = '/' + raw.split('/').filter(Boolean).slice(-1)[0];
  let body: any = {};
  try {
    body = event?.body ? JSON.parse(event.body) : {};
  } catch {
    return reply(400, { error: 'Invalid JSON body.' });
  }

  let caller: Caller;
  try {
    caller = await callerFrom(event);
  } catch (err: any) {
    return reply(err.status ?? 401, { error: err.message });
  }

  try {
    switch (route) {
      case '/users':
        return reply(200, { users: await listUsers() });

      case '/invite': {
        const address = await invite(body.email, body.superadmin === true);
        await audit(caller, 'user.invited', { email: address, superadmin: body.superadmin === true });
        return reply(200, {
          ok: true,
          email: address,
          message: `Invitation sent to ${address}. It carries a temporary password and expires in 7 days.`,
        });
      }

      case '/disable':
        await cognito.send(new AdminDisableUserCommand({
          UserPoolId: USER_POOL_ID, Username: body.username,
        }));
        await audit(caller, 'user.disabled', { username: body.username });
        return reply(200, { ok: true });

      case '/enable':
        await cognito.send(new AdminEnableUserCommand({
          UserPoolId: USER_POOL_ID, Username: body.username,
        }));
        await audit(caller, 'user.enabled', { username: body.username });
        return reply(200, { ok: true });

      case '/resend': {
        await cognito.send(new AdminCreateUserCommand({
          UserPoolId: USER_POOL_ID,
          Username: body.username,
          MessageAction: 'RESEND',
          DesiredDeliveryMediums: ['EMAIL'],
        }));
        await audit(caller, 'user.reinvited', { username: body.username });
        return reply(200, { ok: true, message: `A fresh invitation is on its way to ${body.username}.` });
      }

      case '/role': {
        const { username, superadmin } = body;
        const add = superadmin ? 'superadmin' : 'user';
        const remove = superadmin ? 'user' : 'superadmin';
        await cognito.send(new AdminAddUserToGroupCommand({
          UserPoolId: USER_POOL_ID, Username: username, GroupName: add,
        }));
        await cognito.send(new AdminRemoveUserFromGroupCommand({
          UserPoolId: USER_POOL_ID, Username: username, GroupName: remove,
        })).catch(() => undefined);
        await audit(caller, 'user.role_changed', { username, role: add });
        return reply(200, { ok: true });
      }

      case '/logs':
        return reply(200, {
          entries: await readLog(Number(body.limit ?? 100), body.sub || undefined),
        });

      default:
        return reply(404, { error: `Unknown admin route: ${raw}` });
    }
  } catch (err: any) {
    console.error('admin action failed', route, err);
    const known = err?.name === 'UsernameExistsException'
      ? 'That address already has an account.'
      : `${err?.name ?? 'Error'}: ${err?.message ?? err}`;
    return reply(err.status ?? 500, { error: known });
  }
};
