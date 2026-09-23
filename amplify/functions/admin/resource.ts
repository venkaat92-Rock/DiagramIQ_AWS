import { defineFunction } from '@aws-amplify/backend';

/** The super-admin console's backend: invite, enable/disable, read the log. */
export const adminApi = defineFunction({
  name: 'diagramiq-admin',
  entry: './handler.ts',
  timeoutSeconds: 30,
  memoryMB: 512,
});
