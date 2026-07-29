FROM public.ecr.aws/lambda/nodejs:22 AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm install --no-audit --no-fund
COPY amplify ./amplify
RUN npx esbuild amplify/functions/bedrock-proxy/handler.ts \
      --bundle --platform=node --target=node22 \
      --format=cjs --outfile=/build/handler.js

FROM public.ecr.aws/lambda/nodejs:22
COPY --from=build /build/handler.js ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]
