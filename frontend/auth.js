/* Signing in, without a build step.
 *
 * Cognito's identity provider is a plain JSON API: a POST with an x-amz-target
 * header, no request signing, no SDK. That matters here because this frontend
 * is served as static files with no bundler — pulling in Amplify's JS library
 * to perform SRP would mean adding a build pipeline to a site that does not
 * have one, to do over TLS what this does over TLS.
 *
 * Tokens live in sessionStorage: they go when the tab does, which is the right
 * default for a tool people open on shared machines.
 */

const STORE = 'diagramiq.session';
let cfg = { region: '', clientId: '' };
let session = null;          // { accessToken, idToken, refreshToken, expiresAt, email, groups }

export function configureAuth({ region, userPoolClientId }) {
  cfg = { region: region || '', clientId: userPoolClientId || '' };
  return configured();
}

export const configured = () => Boolean(cfg.region && cfg.clientId);

function idp(target, payload) {
  return fetch(`https://cognito-idp.${cfg.region}.amazonaws.com/`, {
    method: 'POST',
    headers: {
      'content-type': 'application/x-amz-json-1.1',
      'x-amz-target': `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(payload),
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      // Cognito puts the useful half in __type; the message is often empty.
      const kind = String(data.__type || '').split('#').pop();
      throw new Error(friendly(kind, data.message));
    }
    return data;
  });
}

function friendly(kind, message) {
  switch (kind) {
    case 'NotAuthorizedException':
      return 'That email and password do not match an account.';
    case 'UserNotFoundException':
      return 'There is no account for that email address. Ask your administrator for an invitation.';
    case 'UserNotConfirmedException':
      return 'That account has not been confirmed yet. Check the invitation email.';
    case 'InvalidPasswordException':
      return message || 'That password does not meet the policy: at least 12 characters, with upper case, lower case and a number.';
    case 'LimitExceededException':
    case 'TooManyRequestsException':
      return 'Too many attempts. Wait a minute and try again.';
    case 'PasswordResetRequiredException':
      return 'Your password must be reset. Ask your administrator to send a new invitation.';
    default:
      return message || kind || 'Sign-in failed.';
  }
}

/** The payload half of a JWT. Display only — the server verifies the token. */
function claims(token) {
  try {
    const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(part.padEnd(Math.ceil(part.length / 4) * 4, '=')));
  } catch {
    return {};
  }
}

function remember(result, email) {
  const c = claims(result.AccessToken);
  session = {
    accessToken: result.AccessToken,
    idToken: result.IdToken,
    refreshToken: result.RefreshToken || session?.refreshToken || '',
    expiresAt: Date.now() + (result.ExpiresIn || 3600) * 1000 - 60_000,
    email: email || claims(result.IdToken || '').email || c.username || '',
    groups: c['cognito:groups'] || [],
  };
  try {
    sessionStorage.setItem(STORE, JSON.stringify(session));
  } catch { /* private window: the session simply does not survive a reload */ }
  return session;
}

export function restore() {
  if (session) return session;
  try {
    const raw = sessionStorage.getItem(STORE);
    if (raw) session = JSON.parse(raw);
  } catch { session = null; }
  return session;
}

export function signOut() {
  session = null;
  try { sessionStorage.removeItem(STORE); } catch { /* nothing to clear */ }
}

export const currentUser = () => session && { email: session.email, groups: session.groups };
export const isSuperAdmin = () => Boolean(session?.groups?.includes('superadmin'));

/** Sign in. Returns {status:'signedIn'} or {status:'newPassword', session, email}. */
export async function signIn(email, password) {
  const address = String(email || '').trim().toLowerCase();
  const data = await idp('InitiateAuth', {
    AuthFlow: 'USER_PASSWORD_AUTH',
    ClientId: cfg.clientId,
    AuthParameters: { USERNAME: address, PASSWORD: password },
  });
  if (data.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
    // The invitation's temporary password got them this far and stops here.
    return { status: 'newPassword', session: data.Session, email: address };
  }
  if (!data.AuthenticationResult) throw new Error('Cognito returned no tokens.');
  remember(data.AuthenticationResult, address);
  return { status: 'signedIn' };
}

/** Finish the invitation: replace the temporary password with a real one. */
export async function setNewPassword(email, challengeSession, newPassword) {
  const data = await idp('RespondToAuthChallenge', {
    ChallengeName: 'NEW_PASSWORD_REQUIRED',
    ClientId: cfg.clientId,
    Session: challengeSession,
    ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
  });
  if (!data.AuthenticationResult) throw new Error('The password was set but no tokens came back. Sign in again.');
  remember(data.AuthenticationResult, email);
  return { status: 'signedIn' };
}

/** A valid access token, refreshed if it is about to lapse. Null if signed out. */
export async function accessToken() {
  restore();
  if (!session) return null;
  if (Date.now() < session.expiresAt) return session.accessToken;
  if (!session.refreshToken) { signOut(); return null; }
  try {
    const data = await idp('InitiateAuth', {
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: cfg.clientId,
      AuthParameters: { REFRESH_TOKEN: session.refreshToken },
    });
    if (!data.AuthenticationResult) throw new Error('no tokens');
    remember(data.AuthenticationResult, session.email);
    return session.accessToken;
  } catch {
    signOut();
    return null;
  }
}

/** The Authorization header for an API call, or {} when signed out. */
export async function authHeader() {
  const token = await accessToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}
