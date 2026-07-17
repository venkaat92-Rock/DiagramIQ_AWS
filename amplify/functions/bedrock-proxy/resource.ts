import { defineFunction } from '@aws-amplify/backend';

export const bedrockProxy = defineFunction({
  name: 'diagramiq-bedrock-proxy',
  entry: './handler.ts',
  timeoutSeconds: 120,
  memoryMB: 512,
  environment: {
    // Vision-capable model; override per-request from the UI if needed.
    MODEL_ID: 'us.amazon.nova-2-lite-v1:0',
  },
});
