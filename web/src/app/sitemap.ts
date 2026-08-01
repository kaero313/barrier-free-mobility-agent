import type { MetadataRoute } from "next";

const PUBLIC_ROUTES = ["/", "/assistant", "/how-it-works", "/status", "/accessibility"] as const;

export default function sitemap(): MetadataRoute.Sitemap {
  const origin = process.env.WEB_PUBLIC_ORIGIN ?? "http://localhost:3000";
  return PUBLIC_ROUTES.map((path) => ({
    changeFrequency: path === "/status" ? "hourly" : "monthly",
    priority: path === "/" ? 1 : 0.8,
    url: new URL(path, origin).href,
  }));
}
