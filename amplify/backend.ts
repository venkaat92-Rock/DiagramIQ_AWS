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
// src/). Those modules are stdlib-only, so Code.fromAsset ships the folder as
// is: no pip step, no layer. Anything needing binary wheels would want a
// container image instead.
const pythonEngine = new LambdaFunction(apiStack, 'PythonAnalyzer', {
  functionName: 'diagramiq-python-analyzer',
  runtime: Runtime.PYTHON_3_12,
  handler: 'index.handler',
  code: Code.fromAsset('amplify/functions/python-analyzer'),
  timeout: Duration.seconds(60),
  memorySize: 1024, // layout/routing passes over large diagrams
});
const pythonIntegration = new HttpLambdaIntegration(
  'PythonAnalyzerIntegration',
  pythonEngine,
);
for (const path of ['/analyze', '/validate', '/uplift', '/visio', '/normalize']) {
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
