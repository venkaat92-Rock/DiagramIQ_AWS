import { defineBackend } from '@aws-amplify/backend';
import { Stack } from 'aws-cdk-lib';
import {
  CorsHttpMethod,
  HttpApi,
  HttpMethod,
} from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
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

// Expose the endpoint to the frontend via amplify_outputs.json.
backend.addOutput({
  custom: {
    diagramiqApiUrl: httpApi.apiEndpoint,
    region: Stack.of(apiStack).region,
  },
});
