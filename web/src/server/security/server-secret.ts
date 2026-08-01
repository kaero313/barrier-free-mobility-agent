import "server-only";

const MIN_SECRET_BYTES = 32;
const MAX_SECRET_BYTES = 4_096;
const VISIBLE_ASCII = /^[\x21-\x7e]+$/;

interface SecretEnvironment {
  readonly [key: string]: string | undefined;
}

interface ReadServerSecretOptions {
  headerSafe?: boolean;
}

export function readRequiredServerSecret(
  environment: SecretEnvironment,
  name: string,
  options: ReadServerSecretOptions = {},
): string {
  const value = environment[name];
  if (value === undefined || value === "") {
    throw new Error(`${name} is required.`);
  }
  if (value.trim() !== value || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new Error(`${name} must not contain surrounding whitespace or control characters.`);
  }

  const byteLength = Buffer.byteLength(value, "utf8");
  if (byteLength < MIN_SECRET_BYTES || byteLength > MAX_SECRET_BYTES) {
    throw new Error(
      `${name} must contain between ${MIN_SECRET_BYTES} and ${MAX_SECRET_BYTES} UTF-8 bytes.`,
    );
  }
  if (options.headerSafe === true && !VISIBLE_ASCII.test(value)) {
    throw new Error(`${name} must contain visible ASCII characters for safe HTTP transport.`);
  }
  return value;
}
