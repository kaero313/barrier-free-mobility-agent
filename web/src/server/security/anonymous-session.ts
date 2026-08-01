import "server-only";

import {
  createHmac,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";

import { readRequiredServerSecret } from "./server-secret";

export const ANONYMOUS_SESSION_COOKIE_NAME = "bfm_anon_session";
export const HOSTED_ANONYMOUS_SESSION_COOKIE_NAME = "__Host-bfm_anon_session";

const COOKIE_VERSION = "v1";
const NONCE_BYTES = 32;
const SIGNATURE_BYTES = 32;
const BASE64URL_32_BYTES_LENGTH = 43;
const COOKIE_TOKEN_PATTERN = new RegExp(
  `^${COOKIE_VERSION}\\.([A-Za-z0-9_-]{${BASE64URL_32_BYTES_LENGTH}})\\.([A-Za-z0-9_-]{${BASE64URL_32_BYTES_LENGTH}})$`,
);
const COOKIE_SIGNING_CONTEXT = "barrier-free-mobility:anonymous-session:v1";
const OWNER_DERIVATION_CONTEXT = "barrier-free-mobility:agent-owner:v1";

export interface AnonymousSessionEnvironment {
  readonly [key: string]: string | undefined;
  readonly AGENT_ANONYMOUS_SESSION_SECRET?: string;
  readonly NODE_ENV?: string;
  readonly WEB_APP_ENV?: string;
  readonly WEB_PUBLIC_ORIGIN?: string;
}

export interface AnonymousSessionConfig {
  readonly secret: string;
  readonly secure: boolean;
  readonly hosted: boolean;
  readonly publicOrigin: string | null;
  readonly cookieName: string;
}

export interface AnonymousSession {
  readonly ownerId: string;
  readonly setCookie: string | null;
}

export function loadAnonymousSessionConfig(
  environment: AnonymousSessionEnvironment = process.env,
): AnonymousSessionConfig {
  const configuredEnvironment = environment.WEB_APP_ENV;
  if (configuredEnvironment === undefined && environment.NODE_ENV === "production") {
    throw new Error("WEB_APP_ENV is required in a production Node runtime.");
  }
  const applicationEnvironment = configuredEnvironment ?? "local";
  if (!["local", "staging", "production"].includes(applicationEnvironment)) {
    throw new Error("WEB_APP_ENV must be local, staging, or production.");
  }

  const hosted = applicationEnvironment !== "local";
  return {
    secret: readRequiredServerSecret(environment, "AGENT_ANONYMOUS_SESSION_SECRET"),
    secure: hosted,
    hosted,
    publicOrigin: parsePublicOrigin(environment.WEB_PUBLIC_ORIGIN, hosted),
    cookieName: hosted
      ? HOSTED_ANONYMOUS_SESSION_COOKIE_NAME
      : ANONYMOUS_SESSION_COOKIE_NAME,
  };
}

function parsePublicOrigin(rawValue: string | undefined, required: boolean): string | null {
  if (rawValue === undefined || rawValue.trim() === "") {
    if (required) throw new Error("WEB_PUBLIC_ORIGIN is required outside local mode.");
    return null;
  }
  let origin: URL;
  try {
    origin = new URL(rawValue);
  } catch {
    throw new Error("WEB_PUBLIC_ORIGIN must be an absolute HTTP(S) origin.");
  }
  if (
    !["http:", "https:"].includes(origin.protocol)
    || origin.username !== ""
    || origin.password !== ""
    || origin.pathname !== "/"
    || origin.search !== ""
    || origin.hash !== ""
    || (required && origin.protocol !== "https:")
  ) {
    throw new Error("WEB_PUBLIC_ORIGIN must be an HTTPS origin outside local mode.");
  }
  return origin.origin;
}

export function resolveAnonymousSession(
  request: Request,
  config: AnonymousSessionConfig,
): AnonymousSession {
  const cookieToken = readUniqueCookie(
    request.headers.get("cookie"),
    config.cookieName,
  );
  const verifiedNonce = cookieToken === null
    ? null
    : verifyCookieToken(cookieToken, config.secret);

  if (verifiedNonce !== null) {
    return {
      ownerId: deriveOwnerId(verifiedNonce, config.secret),
      setCookie: null,
    };
  }

  const nonce = randomBytes(NONCE_BYTES).toString("base64url");
  const token = createCookieToken(nonce, config.secret);
  return {
    ownerId: deriveOwnerId(nonce, config.secret),
    setCookie: serializeSessionCookie(token, config),
  };
}

export function verifyCookieToken(token: string, secret: string): string | null {
  const match = COOKIE_TOKEN_PATTERN.exec(token);
  if (match === null) {
    return null;
  }
  const nonce = match[1];
  const encodedSignature = match[2];
  if (nonce === undefined || encodedSignature === undefined) {
    return null;
  }

  const nonceBytes = decodeCanonicalBase64Url(nonce, NONCE_BYTES);
  const providedSignature = decodeCanonicalBase64Url(
    encodedSignature,
    SIGNATURE_BYTES,
  );
  if (nonceBytes === null || providedSignature === null) {
    return null;
  }

  const expectedSignature = signNonce(nonce, secret);
  return timingSafeEqual(providedSignature, expectedSignature) ? nonce : null;
}

export function appendAnonymousSessionCookie(
  response: Response,
  session: AnonymousSession,
): Response {
  if (session.setCookie !== null) {
    response.headers.append("set-cookie", session.setCookie);
  }
  return response;
}

function createCookieToken(nonce: string, secret: string): string {
  return `${COOKIE_VERSION}.${nonce}.${signNonce(nonce, secret).toString("base64url")}`;
}

function signNonce(nonce: string, secret: string): Buffer {
  return createHmac("sha256", secret)
    .update(COOKIE_SIGNING_CONTEXT, "utf8")
    .update("\0", "utf8")
    .update(nonce, "ascii")
    .digest();
}

function deriveOwnerId(nonce: string, secret: string): string {
  return createHmac("sha256", secret)
    .update(OWNER_DERIVATION_CONTEXT, "utf8")
    .update("\0", "utf8")
    .update(nonce, "ascii")
    .digest("hex");
}

function decodeCanonicalBase64Url(
  encoded: string,
  expectedLength: number,
): Buffer | null {
  let decoded: Buffer;
  try {
    decoded = Buffer.from(encoded, "base64url");
  } catch {
    return null;
  }
  if (
    decoded.byteLength !== expectedLength
    || decoded.toString("base64url") !== encoded
  ) {
    return null;
  }
  return decoded;
}

function serializeSessionCookie(token: string, config: AnonymousSessionConfig): string {
  const attributes = [
    `${config.cookieName}=${token}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (config.secure) {
    attributes.push("Secure");
  }
  return attributes.join("; ");
}

function readUniqueCookie(
  cookieHeader: string | null,
  cookieName: string,
): string | null {
  if (cookieHeader === null || cookieHeader.length > 8_192) {
    return null;
  }

  let value: string | null = null;
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 1 || part.slice(0, separator).trim() !== cookieName) {
      continue;
    }
    if (value !== null) {
      return null;
    }
    value = part.slice(separator + 1).trim();
  }
  return value === "" ? null : value;
}
