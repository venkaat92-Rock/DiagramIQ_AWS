import { defineBackend } from '@aws-amplify/backend';
import { Duration, Stack } from 'aws-cdk-lib';
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
import { bedrockProxy } from './functions/bedrock-proxy/resource';

const backend = defineBackend({ bedrockProxy });

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

// Public HTTP API in front of the Lambda (CORS open for the Amplify domain).
const apiStack = backend.createStack('diagramiq-api');
const httpApi = new HttpApi(apiStack, 'DiagramIQHttpApi', {
  apiName: 'diagramiq-api',
  corsPreflight: {
    allowOrigins: ['*'],
    allowMethods: [CorsHttpMethod.POST, CorsHttpMethod.OPTIONS],
    allowHeaders: ['content-type'],
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
  allowedHeaders: ['content-type'],
};
const engineUrl = pythonEngine.addFunctionUrl({
  authType: FunctionUrlAuthType.NONE,
  cors: urlCors,
});
const aiUrl = fn.addFunctionUrl({
  authType: FunctionUrlAuthType.NONE,
  cors: urlCors,
});

// Expose the endpoints to the frontend via amplify_outputs.json.
backend.addOutput({
  custom: {
    diagramiqApiUrl: httpApi.apiEndpoint,
    diagramiqEngineUrl: engineUrl.url,   // Python engine, no 30s ceiling
    diagramiqAiUrl: aiUrl.url,           // /convert and /feedback
    region: Stack.of(apiStack).region,
  },
});
