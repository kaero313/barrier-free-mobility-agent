import { readFile, writeFile } from "node:fs/promises";

import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const checkOnly = process.argv.includes("--check");
const schemaUrl = new URL("../openapi/agent.openapi.json", import.meta.url);
const outputUrl = new URL("../src/contracts/agent-openapi.generated.ts", import.meta.url);

const ast = await openapiTS(schemaUrl, { alphabetize: true });
const generated = `${COMMENT_HEADER}${astToString(ast)}`.replaceAll("\r\n", "\n");

if (checkOnly) {
  let current;
  try {
    current = (await readFile(outputUrl, "utf8")).replaceAll("\r\n", "\n");
  } catch {
    console.error("Generated Agent TypeScript contract is missing. Run npm run contracts:generate.");
    process.exitCode = 1;
  }
  if (current !== undefined && current !== generated) {
    console.error("Generated Agent TypeScript contract is stale. Run npm run contracts:generate.");
    process.exitCode = 1;
  }
} else {
  await writeFile(outputUrl, generated, "utf8");
  console.log("Generated src/contracts/agent-openapi.generated.ts");
}
