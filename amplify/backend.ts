import { defineBackend } from '@aws-amplify/backend';
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  CorsHttpMethod,
  HttpApi,
  HttpMethod,
} from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
import {
  CfnFunction,
  Code,
  FunctionUrlAuthType,
  Function as LambdaFunction,
  HttpMethod as FnUrlMethod,
  Runtime,
} from 'aws-cdk-lib/aws-lambda';
import { HttpJwtAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { AttributeType, BillingMode, ProjectionType, Table } from 'aws-cdk-lib/aws-dynamodb';
import { auth } from './auth/resource';
import { adminApi } from './functions/admin/resource';
import { bedrockProxy } from './functions/bedrock-proxy/resource';

const backend = defineBackend({ auth, adminApi, bedrockProxy });

// ── Who may use this, and what they did ──────────────────────────────────────
//
// Accounts are created by a super admin, never by self sign-up: this is an
// internal tool on a public URL. Cognito's own invitation mail carries the
// temporary password, and the first sign-in forces a real one.
const userPool = backend.auth.resources.userPool;
const userPoolClient = backend.auth.resources.userPoolClient;

// Self sign-up off, at the pool rather than in the UI — a hidden form is not a
// control.
const cfnUserPool = backend.auth.resources.cfnResources.cfnUserPool;
cfnUserPool.adminCreateUserConfig = {
  allowAdminCreateUserOnly: true,
  inviteMessageTemplate: undefined,        // defineAuth supplies the wording
};
cfnUserPool.policies = {
  passwordPolicy: {
    minimumLength: 12,
    requireLowercase: true,
    requireUppercase: true,
    requireNumbers: true,
    requireSymbols: false,
    temporaryPasswordValidityDays: 7,
  },
};

// USER_PASSWORD_AUTH lets the browser sign in with a plain JSON POST to
// Cognito. The frontend has no bundler, so pulling in Amplify's JS SDK to do
// SRP would mean adding a build step to a static site; this keeps sign-in to
// fetch() over TLS, which is what the SDK would do underneath anyway.
const cfnClient = backend.auth.resources.cfnResources.cfnUserPoolClient;
cfnClient.explicitAuthFlows = [
  'ALLOW_USER_PASSWORD_AUTH',
  'ALLOW_REFRESH_TOKEN_AUTH',
];

// One row per request: who, which route, when, how long, and a shallow summary
// of the input. Never the document itself — a customer's process map is not
// something to keep a second copy of for the sake of a log.
const auditStack = backend.createStack('diagramiq-audit');
const auditTable = new Table(auditStack, 'AuditLog', {
  tableName: 'diagramiq-audit-log',
  partitionKey: { name: 'pk', type: AttributeType.STRING },
  sortKey: { name: 'sk', type: AttributeType.STRING },
  billingMode: BillingMode.PAY_PER_REQUEST,
  timeToLiveAttribute: 'expiresAt',
  // The log outlives a stack rebuild on purpose: it is the record of who did
  // what, and a teardown should not quietly erase it.
  removalPolicy: RemovalPolicy.RETAIN,
});
// "Everything that happened on a day", without scanning the table.
auditTable.addGlobalSecondaryIndex({
  indexName: 'byDay',
  partitionKey: { name: 'day', type: AttributeType.STRING },
  sortKey: { name: 'ts', type: AttributeType.STRING },
  projectionType: ProjectionType.ALL,
});

// Concurrent executions reserved for each DiagramIQ function. Generous for a
// tool a handful of reviewers use at once, and small against the account pool.
const RESERVED = 10;

// Allow the proxy to call Bedrock models (incl. cross-region inference
// profiles). Tighten `resources` to specific model ARNs if your org requires.
const fn = backend.bedrockProxy.resources.lambda;
fn.addToRolePolicy(
  new PolicyStatement({
    actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    resources: ['*'],
  }),
);

// The proxy is Amplify-managed, so its reservation goes on the underlying
// resource. Guarded rather than cast: if a future Amplify version stops
// exposing a CfnFunction here, this should quietly do nothing rather than fail
// the synth of the whole backend.
const proxyResource = fn.node.defaultChild;
if (proxyResource instanceof CfnFunction) {
  proxyResource.reservedConcurrentExecutions = RESERVED;
}

// Both request-handling functions learn who the caller is and write what they
// did. REQUIRE_AUTH ships 'false': the login layer deploys before it is
// enforced, so a mail-delivery problem cannot lock everyone — including the
// administrator — out of a running tool. Flipping it is its own change.
const REQUIRE_AUTH = process.env.DIAGRAMIQ_REQUIRE_AUTH ?? 'false';
const authEnv = {
  REQUIRE_AUTH,
  USER_POOL_ID: userPool.userPoolId,
  USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
  AUDIT_TABLE: auditTable.tableName,
};
// Amplify types these as IFunction, which has no addEnvironment; the concrete
// construct underneath does.
const proxyFn = fn as LambdaFunction;
for (const [key, value] of Object.entries(authEnv)) {
  proxyFn.addEnvironment(key, value);
}
auditTable.grantWriteData(fn);

// Public HTTP API in front of the Lambda (CORS open for the Amplify domain).
const apiStack = backend.createStack('diagramiq-api');
const httpApi = new HttpApi(apiStack, 'DiagramIQHttpApi', {
  apiName: 'diagramiq-api',
  corsPreflight: {
    allowOrigins: ['*'],
    allowMethods: [CorsHttpMethod.POST, CorsHttpMethod.OPTIONS],
    // The browser sends its token on every call, so the preflight has to allow
    // the header. Leave it out and every authenticated request fails before it
    // is made — as a CORS error with no status, which reads like an outage.
    allowHeaders: ['content-type', 'authorization'],
  },
});
const integration = new HttpLambdaIntegration('BedrockProxyIntegration', fn);
httpApi.addRoutes({ path: '/convert', methods: [HttpMethod.POST], integration });
httpApi.addRoutes({ path: '/feedback', methods: [HttpMethod.POST], integration });

// Python engine Lambda. Amplify Gen 2's defineFunction is Node-only, so this
// is declared straight in CDK — same pipeline, same HTTP API. It carries the
// desktop app's BPMN engine (diagramiq/, ported verbatim from Diagram.IQ
// src/): the rule engines are stdlib-only, the Excel ones use openpyxl (pip
// -installed into vendor/ by amplify.yml), and the AI passes go through
// Bedrock via boto3, which ships with the runtime.
const pythonEngine = new LambdaFunction(apiStack, 'PythonAnalyzer', {
  functionName: 'diagramiq-python-analyzer',
  runtime: Runtime.PYTHON_3_12,
  handler: 'index.handler',
  code: Code.fromAsset('amplify/functions/python-analyzer'),
  // The AI passes are long, but no single one is 5 minutes any more: the
  // compliance audit is sliced into 20-rule batches by the caller rather than
  // scoring all 76 in one generation.
  timeout: Duration.seconds(180),
  // Stated, not inherited.
  //
  // This function was found with reserved concurrency of 0 — the Lambda
  // console's "Throttle" button, or an account janitor, sets exactly that, and
  // the effect is total: every request is refused with 429 before the function
  // runs, so there are no logs, no invocations and no trace of a cause. The
  // account had 971 of 1000 slots free the whole time.
  //
  // Declaring a real reservation makes that state impossible to reach silently:
  // it is visible in the diff, and any redeploy restores it. It also caps this
  // function at RESERVED concurrent executions, which is far more than a review
  // tool needs and leaves the rest of the shared account untouched.
  reservedConcurrentExecutions: RESERVED,
  memorySize: 1024,
  environment: {
    MODEL_ID: 'us.anthropic.claude-opus-4-5-20251101-v1:0',
  },
});
// Same Bedrock grant the Node proxy gets — the AI passes call Converse
// directly rather than hopping through it.
pythonEngine.addToRolePolicy(
  new PolicyStatement({
    actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    resources: ['*'],
  }),
);
for (const [key, value] of Object.entries(authEnv)) {
  pythonEngine.addEnvironment(key, value);
}
auditTable.grantWriteData(pythonEngine);
// The engine verifies tokens by asking Cognito rather than by carrying a
// crypto library: GetUser takes the caller's own access token, so no IAM
// permission on the pool is needed for it.

const pythonIntegration = new HttpLambdaIntegration(
  'PythonAnalyzerIntegration',
  pythonEngine,
);
// Keep in step with ROUTES in the function's index.py.
for (const path of [
  '/analyze',
  '/validate',
  '/uplift',
  '/visio',
  '/normalize',
  '/excel-to-bpmn',
  '/bpmn-to-excel',
  '/patch',
  '/uplift-report',
  '/ai-uplift',
  '/ai-gateways',
  '/ai-layout',
  '/ai-naming',
  '/ai-compliance',
  '/ai-modeller-inputs',
  '/notes',
  '/sop',
  '/discovery-to-bpmn',
  '/discovery-xlsx',
  '/excel-to-discovery',
  '/model',
]) {
  httpApi.addRoutes({
    path,
    methods: [HttpMethod.POST],
    integration: pythonIntegration,
  });
}

// Direct Function URLs for the AI work.
//
// An HTTP API cuts its integration off at 30 seconds and answers a bare 503 —
// no error body, nothing in the Lambda's log, because the Lambda is still
// running. The AI passes routinely exceed that: the compliance audit reasons
// over all 76 rules in one call, and the gap scan reads the whole diagram. A
// Function URL has no such ceiling, so the function's own 300s timeout is what
// applies. The gateway keeps serving the fast, deterministic routes, and the
// frontend falls back to it if these outputs are missing.
//
// CORS is declared here, exactly as it is on the HTTP API above. A browser
// sends a preflight before any JSON POST, and an endpoint that does not answer
// it fails the request before the function is ever reached — which surfaces as
// "Failed to fetch", with no status code and nothing in the function's log.
//
// An earlier version left this off and relied on the handlers' own CORS
// headers. That was wrong, and the evidence was already in this file: the HTTP
// API has always declared CORS *and* had handlers that return the same
// headers, and that combination has worked from the start.
//
// The two endpoints spell CORS differently, and that asymmetry is correct.
//
// An HTTP API answers the preflight only for methods it was told to allow, so
// OPTIONS has to be listed above. A Function URL answers the preflight itself,
// in the service, before the function is reached — Cors.AllowMethods describes
// the *actual* request, and its valid values are GET | PUT | HEAD | POST |
// PATCH | DELETE | *. OPTIONS is not one of them.
//
// Listing it here does not tighten anything; it makes the stack undeployable.
// CreateFunctionUrlConfig rejects the value, the CloudFormation update rolls
// back, and the Amplify build fails before the frontend is published — which
// is exactly what happened to Deployment 27. The site kept serving the last
// good build, so the symptom was "my fix changed nothing", not an error.
const urlCors = {
  allowedOrigins: ['*'],
  allowedMethods: [FnUrlMethod.POST],
  allowedHeaders: ['content-type', 'authorization'],
};
const engineUrl = pythonEngine.addFunctionUrl({
  authType: FunctionUrlAuthType.NONE,
  cors: urlCors,
});
const aiUrl = fn.addFunctionUrl({
  authType: FunctionUrlAuthType.NONE,
  cors: urlCors,
});

// ── The super-admin console's backend ────────────────────────────────────────
const adminFn = backend.adminApi.resources.lambda as LambdaFunction;
adminFn.addEnvironment('USER_POOL_ID', userPool.userPoolId);
adminFn.addEnvironment('USER_POOL_CLIENT_ID', userPoolClient.userPoolClientId);
adminFn.addEnvironment('AUDIT_TABLE', auditTable.tableName);
auditTable.grantReadWriteData(adminFn);
adminFn.addToRolePolicy(
  new PolicyStatement({
    actions: [
      'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminDisableUser',
      'cognito-idp:AdminEnableUser',
      'cognito-idp:AdminAddUserToGroup',
      'cognito-idp:AdminRemoveUserFromGroup',
      'cognito-idp:AdminListGroupsForUser',
      'cognito-idp:ListUsers',
    ],
    resources: [userPool.userPoolArn],
  }),
);
const adminUrl = adminFn.addFunctionUrl({
  authType: FunctionUrlAuthType.NONE,      // the handler refuses anyone who is
  cors: urlCors,                           // not a verified super admin
});

// Expose the endpoints to the frontend via amplify_outputs.json.
backend.addOutput({
  custom: {
    diagramiqApiUrl: httpApi.apiEndpoint,
    diagramiqEngineUrl: engineUrl.url,   // Python engine, no 30s ceiling
    diagramiqAiUrl: aiUrl.url,           // /convert and /feedback
    diagramiqAdminUrl: adminUrl.url,     // super-admin console
    userPoolId: userPool.userPoolId,
    userPoolClientId: userPoolClient.userPoolClientId,
    requireAuth: REQUIRE_AUTH,
    region: Stack.of(apiStack).region,
  },
});
