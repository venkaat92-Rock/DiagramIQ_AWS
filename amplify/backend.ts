import { defineBackend } from '@aws-amplify/backend';
import { Duration, Stack } from 'aws-cdk-lib';
import {
  CorsHttpMethod,
  HttpApi,
  HttpMethod,
} from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { bedrockProxy } from './functions/bedrock-proxy/resource';

const backend = defineBackend({ bedrockProxy });

// Allow the proxy to call Bedrock models (incl. cross-region inference
// profiles). Tighten `resources` to specific model ARNs if your org requires.
const fn = backend.bedrockProxy.resources.lambda;
fn.addToRolePolicy(
  new PolicyStatement({
    actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    resources: ['*'],
  }),
);

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
  // The AI passes are long: the compliance audit reasons over all 76 Auspost
  // rules, and layout cleanup ships the whole diagram both ways.
  timeout: Duration.seconds(300),
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
]) {
  httpApi.addRoutes({
    path,
    methods: [HttpMethod.POST],
    integration: pythonIntegration,
  });
}

// Expose the endpoint to the frontend via amplify_outputs.json.
backend.addOutput({
  custom: {
    diagramiqApiUrl: httpApi.apiEndpoint,
    region: Stack.of(apiStack).region,
  },
});
