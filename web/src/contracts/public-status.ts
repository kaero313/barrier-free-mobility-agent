import { z } from "zod";

const PublicCheckedAtSchema = z.string().datetime({ offset: true }).nullable();
const PublicServiceAvailabilitySchema = z.enum([
  "available",
  "unavailable",
  "unknown",
]);
const PublicFeatureAvailabilitySchema = z.enum([
  "available",
  "not_supported",
  "temporarily_unavailable",
  "unknown",
]);

const AgentServiceStatusSchema = z
  .object({
    id: z.literal("agent"),
    label: z.string().min(1).max(80),
    status: PublicServiceAvailabilitySchema,
  })
  .strict();

const MobilityInformationServiceStatusSchema = z
  .object({
    id: z.literal("mobility_information"),
    label: z.string().min(1).max(80),
    status: PublicServiceAvailabilitySchema,
  })
  .strict();

const TripAccessibilityFeatureStatusSchema = z
  .object({
    id: z.literal("trip_accessibility"),
    label: z.string().min(1).max(80),
    status: PublicFeatureAvailabilitySchema,
  })
  .strict();

const StationFacilityFeatureStatusSchema = z
  .object({
    id: z.literal("station_facility"),
    label: z.string().min(1).max(80),
    status: PublicFeatureAvailabilitySchema,
  })
  .strict();

/**
 * Web-owned public projection. This contract deliberately does not extend the
 * Agent readiness or future operations contracts.
 */
export const PublicServiceStatusSchema = z
  .object({
    schema_version: z.literal("1"),
    status: z.enum(["operational", "degraded", "unavailable", "unknown"]),
    checked_at: PublicCheckedAtSchema,
    message: z.string().min(1).max(240),
    services: z.tuple([
      AgentServiceStatusSchema,
      MobilityInformationServiceStatusSchema,
    ]),
    features: z.tuple([
      TripAccessibilityFeatureStatusSchema,
      StationFacilityFeatureStatusSchema,
    ]),
  })
  .strict();

export type PublicServiceStatus = z.infer<typeof PublicServiceStatusSchema>;

