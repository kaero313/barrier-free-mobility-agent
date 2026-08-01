import { z } from "zod";

import type { components } from "./agent-openapi.generated";

export const AgentIdSchema = z.string().regex(/^[a-f0-9]{32}$/);
export const RequestIdSchema = z.string().regex(/^[A-Za-z0-9._-]{1,128}$/);

const NullableDateTimeSchema = z
  .string()
  .datetime({ offset: true, local: true })
  .nullable();
const ResponseStatusSchema = z.enum([
  "SUCCESS",
  "PARTIAL",
  "FAILED",
  "NEEDS_CLARIFICATION",
]);
const QuestionIntentSchema = z.enum([
  "trip_accessibility",
  "facility_status",
  "alternative_request",
  "unknown",
]);
const RiskLevelSchema = z.enum(["LOW", "CAUTION", "HIGH", "UNKNOWN"]);
const ConfidenceLevelSchema = z.enum(["HIGH", "MEDIUM", "LOW"]);
const AnswerSourceSchema = z.enum(["mcp", "agent"]);
const GraphTurnStatusSchema = z.enum(["completed", "interrupted", "failed"]);
const FacilityTypeSchema = z.enum([
  "ELEVATOR",
  "ESCALATOR",
  "ACCESSIBLE_RESTROOM",
  "RESTROOM",
  "WHEELCHAIR_CHARGER",
  "UNKNOWN",
]);
const FacilityStatusSchema = z.enum([
  "AVAILABLE",
  "UNAVAILABLE",
  "MAINTENANCE",
  "UNKNOWN",
]);
const FacilityQuestionKindSchema = z.enum([
  "status",
  "location",
  "existence",
  "overview",
]);
const ClarificationFieldSchema = z.enum([
  "origin",
  "destination",
  "wheelchair",
  "stroller",
  "cane_or_walker",
  "can_use_stairs",
  "can_use_escalator",
  "need_elevator_only",
  "need_accessible_restroom",
  "need_wheelchair_charger",
  "avoid_many_transfers",
  "target_station",
  "target_line",
  "facility_types",
  "facility_question_kind",
  "accessible_restroom_confirmation",
  "intent",
  "other",
]);

export const StartRunRequestSchema = z
  .object({
    question: z.string().min(1).max(20_000),
  })
  .strict();

export type StartRunRequest = z.infer<typeof StartRunRequestSchema>;

export const LegacyResumeRunRequestSchema = z
  .object({
    answer: z.string().min(1).max(20_000),
    clarification_id: z.string().min(1).max(200).nullable().optional(),
  })
  .strict();

const StructuredTextAnswerValueSchema = z
  .object({
    kind: z.literal("text"),
    value: z.string().min(1).max(2_000),
  })
  .strict();

const StructuredChoiceAnswerValueSchema = z
  .object({
    kind: z.literal("choice"),
    value: z.string().min(1).max(200),
  })
  .strict();

const StructuredTriStateAnswerValueSchema = z
  .object({
    kind: z.literal("tri_state"),
    value: z.enum(["yes", "no", "unknown"]),
  })
  .strict();

export const StructuredClarificationAnswerSchema = z.discriminatedUnion("mode", [
  z
    .object({
      mode: z.literal("single_text"),
      value: z.string().min(1).max(20_000),
    })
    .strict(),
  z
    .object({
      mode: z.literal("fields"),
      values: z
        .array(
          z
            .object({
              input_id: z.string().min(1).max(100),
              value: z.discriminatedUnion("kind", [
                StructuredTextAnswerValueSchema,
                StructuredChoiceAnswerValueSchema,
                StructuredTriStateAnswerValueSchema,
              ]),
            })
            .strict(),
        )
        .min(1)
        .max(16),
    })
    .strict()
    .superRefine((value, context) => {
      if (new Set(value.values.map((item) => item.input_id)).size !== value.values.length) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["values"],
          message: "Structured clarification answer input IDs must be unique.",
        });
      }
    }),
]);

export type StructuredClarificationAnswer = z.infer<
  typeof StructuredClarificationAnswerSchema
>;

export const StructuredResumeRunRequestSchema = z
  .object({
    schema_version: z.literal("2"),
    clarification_id: z.string().min(1).max(200),
    answer: StructuredClarificationAnswerSchema,
  })
  .strict();

/**
 * The BFF accepts the legacy request during the schema migration. The browser
 * client only emits the structured v2 branch.
 */
export const ResumeRunRequestSchema = z.union([
  StructuredResumeRunRequestSchema,
  LegacyResumeRunRequestSchema,
]);

export type StructuredResumeRunRequest = z.infer<
  typeof StructuredResumeRunRequestSchema
>;
// JSON cannot carry an explicit `undefined`. This hand-written legacy type
// mirrors OpenAPI's exact optional property semantics more precisely than
// Zod's inferred `{ clarification_id?: T | undefined }` output type.
export type LegacyResumeRunRequest = {
  answer: string;
  clarification_id?: string | null;
};
export type ResumeRunRequest = LegacyResumeRunRequest | StructuredResumeRunRequest;

export const CreateThreadResponseSchema = z
  .object({
    schema_version: z.literal("1"),
    request_id: RequestIdSchema,
    thread_id: AgentIdSchema,
    persistence: z.enum(["ephemeral", "durable"]),
  })
  .strict();

export type CreateThreadResponse = z.infer<typeof CreateThreadResponseSchema>;

const FailedSourceSummarySchema = z
  .object({
    source_name: z.enum([
      "route_information",
      "station_information",
      "facility_information",
      "mobility_information",
    ]),
    display_name: z.string().min(1).max(80),
    recoverable: z.boolean(),
  })
  .strict();

const AnswerEvidenceSchema = z
  .object({
    risk_level: RiskLevelSchema.nullable(),
    confidence_level: ConfidenceLevelSchema.nullable(),
    failed_sources: z.array(FailedSourceSummarySchema),
    limitations: z.array(z.string()),
    unverified_parts: z.array(z.string()),
    last_checked_at: NullableDateTimeSchema,
  })
  .strict();

export const ClarificationOptionSchema = z
  .object({
    value: z.string().min(1),
    label: z.string().min(1),
  })
  .strict();

const ClarificationPromptSchema = z
  .object({
    id: z.string().min(1).max(200),
    question: z.string().min(1),
    fields: z.array(ClarificationFieldSchema),
  })
  .strict();

const ClarificationInputCommonSchema = z.object({
  id: z.string().min(1).max(100),
  prompt_id: z.string().min(1).max(200),
  field: ClarificationFieldSchema,
  label: z.string().min(1).max(100),
  required: z.literal(true),
});

const TextClarificationInputSchema = ClarificationInputCommonSchema.extend({
  control: z.literal("text"),
  min_length: z.literal(1),
  max_length: z.number().int().min(1).max(2_000),
})
  .strict()
  .superRefine((value, context) => {
    if (value.min_length > value.max_length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["max_length"],
        message: "Text clarification max_length must be at least min_length.",
      });
    }
  });

const TriStateClarificationOptionSchema = z
  .object({
    value: z.enum(["yes", "no", "unknown"]),
    label: z.string().min(1).max(200),
  })
  .strict();

const StructuredClarificationOptionSchema = z
  .object({
    value: z.string().min(1).max(200),
    label: z.string().min(1).max(200),
  })
  .strict();

const TriStateClarificationInputSchema = ClarificationInputCommonSchema.extend({
  control: z.literal("tri_state"),
  options: z.array(TriStateClarificationOptionSchema).length(3),
})
  .strict()
  .superRefine((value, context) => {
    const values = value.options.map((option) => option.value);
    if (values.join("\u0000") !== "yes\u0000no\u0000unknown") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["options"],
        message: "Tri-state options must be ordered yes, no, unknown.",
      });
    }
  });

const SingleChoiceClarificationInputSchema = ClarificationInputCommonSchema.extend({
  control: z.literal("single_choice"),
  options: z.array(StructuredClarificationOptionSchema).min(1).max(20),
})
  .strict()
  .superRefine((value, context) => {
    if (new Set(value.options.map((option) => option.value)).size !== value.options.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["options"],
        message: "Single-choice option values must be unique.",
      });
    }
  });

export const ClarificationInputSchema = z.discriminatedUnion("control", [
  TextClarificationInputSchema,
  TriStateClarificationInputSchema,
  SingleChoiceClarificationInputSchema,
]);

const SingleTextAnswerRequestSchema = z
  .object({
    mode: z.literal("single_text"),
    label: z.string().min(1).max(100),
    min_length: z.literal(1),
    max_length: z.number().int().min(1).max(20_000),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.min_length > value.max_length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["max_length"],
        message: "Single-text max_length must be at least min_length.",
      });
    }
  });

const FieldsAnswerRequestSchema = z
  .object({
    mode: z.literal("fields"),
    inputs: z.array(ClarificationInputSchema).min(1).max(16),
  })
  .strict();

export const ClarificationAnswerRequestSchema = z.discriminatedUnion("mode", [
  SingleTextAnswerRequestSchema,
  FieldsAnswerRequestSchema,
]);

export const ClarificationViewSchema = z
  .object({
    schema_version: z.literal("2"),
    id: z.string().min(1).max(200),
    prompts: z.array(ClarificationPromptSchema).min(1),
    answer_request: ClarificationAnswerRequestSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (new Set(value.prompts.map((prompt) => prompt.id)).size !== value.prompts.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Clarification prompt IDs must be unique.",
      });
    }
    if (value.answer_request.mode !== "fields") {
      return;
    }
    const promptIds = new Set(value.prompts.map((prompt) => prompt.id));
    const inputIds = value.answer_request.inputs.map((input) => input.id);
    if (new Set(inputIds).size !== inputIds.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["answer_request", "inputs"],
        message: "Clarification input IDs must be unique.",
      });
    }
    const inputFields = value.answer_request.inputs.map((input) => input.field);
    if (new Set(inputFields).size !== inputFields.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["answer_request", "inputs"],
        message: "Clarification fields must be answered by one input each.",
      });
    }
    for (const [index, input] of value.answer_request.inputs.entries()) {
      const prompt = value.prompts.find((candidate) => candidate.id === input.prompt_id);
      if (!promptIds.has(input.prompt_id) || prompt === undefined) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["answer_request", "inputs", index, "prompt_id"],
          message: "Clarification input must reference an active prompt.",
        });
      } else if (!prompt.fields.includes(input.field)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["answer_request", "inputs", index, "field"],
          message: "Clarification input field must belong to its prompt.",
        });
      }
    }
  });

export type ClarificationView = z.infer<typeof ClarificationViewSchema>;

const ConversationLocationViewSchema = z
  .object({
    query: z.string().min(1),
    station_name: z.string().nullable(),
    line: z.string().nullable(),
    label: z.string().min(1),
    status: z.enum(["provided", "resolved"]),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.status === "resolved" && value.station_name === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["station_name"],
        message: "Resolved location requires station_name.",
      });
    }
  });

const ContextProvenanceSchema = z.enum([
  "user_provided",
  "user_confirmed",
  "previous_context",
]);

const MobilityConditionViewSchema = z
  .object({
    field: ClarificationFieldSchema,
    label: z.string().min(1),
    value: z.boolean(),
    provenance: ContextProvenanceSchema,
    provenance_label: z.string().min(1),
  })
  .strict();

const ActiveClarificationSummarySchema = z
  .object({
    id: z.string().min(1).max(200),
    prompt_ids: z.array(z.string()).min(1),
    fields: z.array(ClarificationFieldSchema).min(1),
  })
  .strict();

const FacilityContextViewSchema = z
  .object({
    target: ConversationLocationViewSchema.nullable(),
    target_provenance: ContextProvenanceSchema.nullable(),
    target_provenance_label: z.string().nullable(),
    requested_types: z.array(FacilityTypeSchema),
    requested_types_provenance: ContextProvenanceSchema.nullable(),
    requested_types_provenance_label: z.string().nullable(),
    question_kind: FacilityQuestionKindSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if ((value.target_provenance === null) !== (value.target_provenance_label === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Target provenance and label must appear together.",
      });
    }
    if (
      (value.requested_types_provenance === null) !==
      (value.requested_types_provenance_label === null)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Requested type provenance and label must appear together.",
      });
    }
  });

export const ConversationContextViewSchema = z
  .object({
    schema_version: z.literal("2"),
    intent: QuestionIntentSchema,
    origin: ConversationLocationViewSchema.nullable(),
    destination: ConversationLocationViewSchema.nullable(),
    mobility_conditions: z.array(MobilityConditionViewSchema),
    facility: FacilityContextViewSchema.nullable(),
    active_clarification: ActiveClarificationSummarySchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    const hasTripContext =
      value.origin !== null || value.destination !== null || value.mobility_conditions.length > 0;
    if (value.intent === "trip_accessibility" && value.facility !== null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Trip intent cannot expose facility context." });
    } else if (value.intent === "facility_status" && hasTripContext) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Facility intent cannot expose trip context." });
    } else if (
      value.intent !== "trip_accessibility" &&
      value.intent !== "facility_status" &&
      (hasTripContext || value.facility !== null)
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Unsupported intent cannot expose domain context." });
    }
  });

export type ConversationContextView = z.infer<typeof ConversationContextViewSchema>;

const EvidenceSourceSummarySchema = z
  .object({
    display_name: z.string(),
    source_type: z.enum(["public_api", "cache"]),
    checked_at: NullableDateTimeSchema,
    success: z.boolean(),
  })
  .strict();

const TripAnswerPresentationSchema = z
  .object({
    schema_version: z.literal("2"),
    kind: z.literal("trip"),
    trip: z
      .object({
        origin: z.string(),
        destination: z.string(),
        route_summary: z.string(),
      })
      .strict(),
    safety_notice: z.string(),
    confidence_reasons: z.array(z.string()),
    evidence_sources: z.array(EvidenceSourceSummarySchema),
  })
  .strict();

const FacilityDetailViewSchema = z
  .object({
    station_name: z.string(),
    line: z.string().nullable(),
    facility_type: FacilityTypeSchema,
    status: FacilityStatusSchema,
    location_description: z.string().nullable(),
    inside_gate: z.boolean().nullable(),
    open_time: z.string().nullable(),
    has_emergency_bell: z.boolean().nullable(),
  })
  .strict();

const FacilityItemViewSchema = z
  .object({
    facility_type: FacilityTypeSchema,
    answer_state: z.enum([
      "AVAILABLE",
      "MIXED",
      "MAINTENANCE",
      "UNAVAILABLE",
      "NOT_CONFIRMED",
      "UNKNOWN",
    ]),
    facilities: z.array(FacilityDetailViewSchema),
    total_count: z.number().int().nonnegative(),
    truncated: z.boolean(),
  })
  .strict();

const FacilityAnswerPresentationSchema = z
  .object({
    schema_version: z.literal("2"),
    kind: z.literal("facility"),
    facility: z
      .object({
        station_name: z.string(),
        line: z.string().nullable(),
        question_kind: FacilityQuestionKindSchema,
        items: z.array(FacilityItemViewSchema),
      })
      .strict(),
    safety_notice: z.string(),
    evidence_sources: z.array(EvidenceSourceSummarySchema),
  })
  .strict();

export const AnswerPresentationSchema = z.discriminatedUnion("kind", [
  TripAnswerPresentationSchema,
  FacilityAnswerPresentationSchema,
]);

export type AnswerPresentation = z.infer<typeof AnswerPresentationSchema>;

export const ConversationRunResponseSchema = z
  .object({
    schema_version: z.literal("4"),
    request_id: RequestIdSchema,
    thread_id: AgentIdSchema,
    run_id: AgentIdSchema,
    run_status: GraphTurnStatusSchema,
    source: AnswerSourceSchema,
    status: ResponseStatusSchema,
    intent: QuestionIntentSchema,
    user_message: z.string(),
    clarification_needed: z.boolean(),
    clarification: ClarificationViewSchema.nullable(),
    available_partial_info: z.array(z.string()),
    evidence: AnswerEvidenceSchema,
    presentation: AnswerPresentationSchema.nullable(),
    context: ConversationContextViewSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.context.intent !== value.intent) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["context", "intent"], message: "Intent mismatch." });
    }
    if (
      value.run_status === "interrupted" &&
      (value.status !== "NEEDS_CLARIFICATION" || value.clarification === null)
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Interrupted run requires clarification." });
    } else if (value.run_status === "completed" && value.status === "NEEDS_CLARIFICATION") {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Completed run cannot require clarification." });
    } else if (
      value.run_status === "failed" &&
      (value.status !== "FAILED" || value.clarification !== null)
    ) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Failed run requires terminal FAILED outcome." });
    }
    if (value.clarification_needed !== (value.status === "NEEDS_CLARIFICATION")) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["clarification_needed"], message: "Clarification flag mismatch." });
    }
    const active = value.context.active_clarification;
    if (value.clarification !== null) {
      if (active === null || active.id !== value.clarification.id) {
        context.addIssue({ code: z.ZodIssueCode.custom, message: "Active clarification mismatch." });
      }
      const activePromptIds = active?.prompt_ids ?? [];
      const promptIds = value.clarification.prompts.map((prompt) => prompt.id);
      if (JSON.stringify(activePromptIds) !== JSON.stringify(promptIds)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Active clarification prompt IDs do not match the view.",
        });
      }
      const activeFields = active?.fields ?? [];
      const fields = Array.from(
        new Set(value.clarification.prompts.flatMap((prompt) => prompt.fields)),
      );
      if (JSON.stringify(activeFields) !== JSON.stringify(fields)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Active clarification fields do not match the view.",
        });
      }
    } else if (active !== null) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: "Terminal response cannot retain active clarification." });
    }
    if (
      value.presentation !== null &&
      ((value.intent === "trip_accessibility" && value.presentation.kind !== "trip") ||
        (value.intent === "facility_status" && value.presentation.kind !== "facility") ||
        (value.intent !== "trip_accessibility" && value.intent !== "facility_status"))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Presentation kind does not match intent.",
      });
    }
    if (
      value.presentation === null &&
      value.source === "mcp" &&
      value.status !== "NEEDS_CLARIFICATION" &&
      (value.intent === "trip_accessibility" || value.intent === "facility_status")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Terminal MCP response requires a presentation.",
      });
    }
    if (value.presentation?.kind === "facility") {
      const station = value.context.facility?.target?.station_name;
      if (station !== null && station !== undefined && station !== value.presentation.facility.station_name) {
        context.addIssue({ code: z.ZodIssueCode.custom, message: "Facility presentation target mismatch." });
      }
    }
  });

export type ConversationRunResponse = z.infer<typeof ConversationRunResponseSchema>;

const ContractStatusSchema = z.enum([
  "ready",
  "degraded",
  "unavailable",
  "incompatible",
  "unchecked",
]);

export const ReadinessResponseSchema = z
  .object({
    status: z.enum(["ready", "not_ready"]),
    service: z.string(),
    version: z.string(),
    dependencies: z
      .object({
        mobility_mcp: z
          .object({
            status: ContractStatusSchema,
            checked_at: NullableDateTimeSchema,
            tool_count: z.number().int().nonnegative(),
            capabilities: z.array(z.enum(["trip_accessibility", "facility_question"])),
            schema_fingerprint: z.string().nullable(),
            error_code: z.string().nullable(),
          })
          .strict(),
        persistence: z
          .object({
            status: z.enum(["ready", "unavailable"]),
            backend: z.enum(["memory", "postgres"]),
          })
          .strict(),
      })
      .strict(),
  })
  .strict()
  .superRefine((value, context) => {
    const dependenciesReady =
      value.dependencies.mobility_mcp.status === "ready" &&
      value.dependencies.persistence.status === "ready";
    if ((value.status === "ready") !== dependenciesReady) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Overall readiness must match MCP and persistence readiness.",
      });
    }
  });

export type ReadinessResponse = z.infer<typeof ReadinessResponseSchema>;

export const AgentValidationIssueSchema = z
  .object({
    type: z.string().min(1).max(100),
    location: z.array(z.string().min(1).max(100)).min(1).max(16),
    message: z.string().min(1).max(300),
  })
  .strict();

export type AgentValidationIssue = z.infer<typeof AgentValidationIssueSchema>;

export const AgentErrorDetailSchema = z.union([
  z.object({}).strict(),
  z
    .object({
      issues: z.array(AgentValidationIssueSchema).min(1).max(32),
    })
    .strict(),
]);

const AgentErrorBodySchema = z
  .object({
    code: z.string().min(1).max(100).regex(/^[A-Z][A-Z0-9_]*$/),
    message: z.string().min(1).max(500),
    retryable: z.boolean(),
    detail: AgentErrorDetailSchema,
  })
  .strict()
  .superRefine((value, context) => {
    const hasValidationIssues = "issues" in value.detail;
    if ((value.code === "REQUEST_VALIDATION_ERROR") !== hasValidationIssues) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["detail"],
        message:
          "Only REQUEST_VALIDATION_ERROR requires and permits validation issues.",
      });
    }
  });

export const AgentErrorResponseSchema = z
  .object({
    schema_version: z.literal("2"),
    request_id: RequestIdSchema,
    error: AgentErrorBodySchema,
  })
  .strict();

export type AgentErrorResponse = z.infer<typeof AgentErrorResponseSchema>;

type GeneratedSchemas = components["schemas"];
type IsAssignable<Actual, Expected> = [Actual] extends [Expected] ? true : false;
type AssertTrue<Value extends true> = Value;

/**
 * Compile-time proof that every value accepted by the stricter runtime Zod
 * projections is compatible with the FastAPI-generated OpenAPI contract.
 */
export type GeneratedAgentContractCompatibility = [
  AssertTrue<IsAssignable<CreateThreadResponse, GeneratedSchemas["CreateThreadResponse"]>>,
  AssertTrue<IsAssignable<StartRunRequest, GeneratedSchemas["StartRunRequest"]>>,
  AssertTrue<
    IsAssignable<LegacyResumeRunRequest, GeneratedSchemas["ResumeRunRequest"]>
  >,
  AssertTrue<
    IsAssignable<StructuredResumeRunRequest, GeneratedSchemas["ResumeRunRequest"]>
  >,
  AssertTrue<
    IsAssignable<ConversationRunResponse, GeneratedSchemas["ConversationRunResponseV4"]>
  >,
  AssertTrue<IsAssignable<ReadinessResponse, GeneratedSchemas["ReadinessResponse"]>>,
  AssertTrue<IsAssignable<AgentErrorResponse, GeneratedSchemas["ErrorResponse"]>>,
];
