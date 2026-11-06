"use client";

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { ArrowRightIcon, CircleAlertIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import type {
  AgentValidationIssue,
  ClarificationView,
  StructuredClarificationAnswer,
} from "@/contracts/agent";

const SINGLE_TEXT_KEY = "single-text";

type FieldsAnswer = Extract<StructuredClarificationAnswer, { mode: "fields" }>;

export interface ClarificationAnswer {
  answer: StructuredClarificationAnswer;
  displayValue: string;
}

interface ClarificationFormProps {
  clarification: ClarificationView;
  busy: boolean;
  submitIssues: readonly AgentValidationIssue[];
  onEdit: () => void;
  onSubmit: (answer: ClarificationAnswer) => Promise<void>;
}

interface IssueProjection {
  fieldErrors: Record<string, string>;
  generalErrors: string[];
}

function issueProjection(
  clarification: ClarificationView,
  issues: readonly AgentValidationIssue[],
): IssueProjection {
  const fieldErrors: Record<string, string> = {};
  const generalErrors: string[] = [];
  const request = clarification.answer_request;

  for (const issue of issues) {
    if (request.mode === "single_text") {
      fieldErrors[SINGLE_TEXT_KEY] ??= issue.message;
      continue;
    }

    const directInput = request.inputs.find((input) => issue.location.includes(input.id));
    const valuesIndex = issue.location.lastIndexOf("values");
    const positionToken = valuesIndex >= 0 ? issue.location[valuesIndex + 1] : undefined;
    const position = positionToken === undefined ? Number.NaN : Number.parseInt(positionToken, 10);
    const indexedInput = Number.isInteger(position) ? request.inputs[position] : undefined;
    const input = directInput ?? indexedInput;
    if (input === undefined) {
      generalErrors.push(issue.message);
    } else {
      fieldErrors[input.id] ??= issue.message;
    }
  }

  return { fieldErrors, generalErrors: Array.from(new Set(generalErrors)) };
}

// 글자 수는 제한에 가까워졌을 때만 보여 준다. 그 전에는 maxLength 가 넘치지 않게 막는다.
function CharacterCount({ length, max }: { length: number; max: number }) {
  if (length < max * 0.9) return null;
  return (
    <p className="text-right text-xs text-muted-foreground">
      {length.toLocaleString("ko-KR")} / {max.toLocaleString("ko-KR")}자
    </p>
  );
}

function composingEnter(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
  return (
    event.key === "Enter" &&
    (event.nativeEvent.isComposing || event.keyCode === 229)
  );
}

export function ClarificationForm({
  clarification,
  busy,
  submitIssues,
  onEdit,
  onSubmit,
}: ClarificationFormProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const controlRefs = useRef(new Map<string, HTMLElement>());
  const baseId = useId();
  const [singleText, setSingleText] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [validationErrors, setValidationErrors] = useState<Record<string, string>>({});

  const promptDomIds = useMemo(
    () =>
      new Map(
        clarification.prompts.map((prompt, index) => [
          prompt.id,
          `${baseId}-prompt-${index}`,
        ]),
      ),
    [baseId, clarification.prompts],
  );
  const projectedIssues = useMemo(
    () => issueProjection(clarification, submitIssues),
    [clarification, submitIssues],
  );
  const errors = { ...projectedIssues.fieldErrors, ...validationErrors };
  const errorEntries = Object.entries(errors);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    const firstKey = Object.keys(projectedIssues.fieldErrors)[0];
    if (firstKey !== undefined) {
      controlRefs.current.get(firstKey)?.focus();
    }
  }, [projectedIssues]);

  function errorId(key: string): string {
    return `${baseId}-error-${key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
  }

  function describedBy(promptId: string | null, key: string): string | undefined {
    const ids = [
      promptId === null ? undefined : promptDomIds.get(promptId),
      errors[key] === undefined ? undefined : errorId(key),
    ].filter((value): value is string => value !== undefined);
    return ids.length > 0 ? ids.join(" ") : undefined;
  }

  function clearInputError(key: string) {
    setValidationErrors((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    onEdit();
  }

  function focusFirstError(nextErrors: Record<string, string>) {
    const firstKey = Object.keys(nextErrors)[0];
    if (firstKey !== undefined) {
      window.setTimeout(() => controlRefs.current.get(firstKey)?.focus(), 0);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;

    const request = clarification.answer_request;
    if (request.mode === "single_text") {
      const value = singleText.trim();
      const nextErrors: Record<string, string> = {};
      if (value.length < request.min_length) {
        nextErrors[SINGLE_TEXT_KEY] = "계속하려면 추가 정보를 입력해 주세요.";
      } else if (value.length > request.max_length) {
        nextErrors[SINGLE_TEXT_KEY] = `${request.max_length.toLocaleString("ko-KR")}자 이내로 입력해 주세요.`;
      }
      setValidationErrors(nextErrors);
      if (Object.keys(nextErrors).length > 0) {
        focusFirstError(nextErrors);
        return;
      }
      await onSubmit({ answer: { mode: "single_text", value }, displayValue: value });
      return;
    }

    const nextErrors: Record<string, string> = {};
    const values: FieldsAnswer["values"] = [];
    const displayParts: string[] = [];

    for (const input of request.inputs) {
      const rawValue = fieldValues[input.id] ?? "";
      if (input.control === "text") {
        const value = rawValue.trim();
        if (value.length < input.min_length) {
          nextErrors[input.id] = `${input.label}을(를) 입력해 주세요.`;
          continue;
        }
        if (value.length > input.max_length) {
          nextErrors[input.id] = `${input.label}은(는) ${input.max_length.toLocaleString("ko-KR")}자 이내로 입력해 주세요.`;
          continue;
        }
        values.push({ input_id: input.id, value: { kind: "text", value } });
        displayParts.push(`${input.label}: ${value}`);
        continue;
      }

      if (input.control === "tri_state") {
        const selected = input.options.find((option) => option.value === rawValue);
        if (selected === undefined) {
          nextErrors[input.id] = `${input.label} 항목을 선택해 주세요.`;
          continue;
        }
        values.push({
          input_id: input.id,
          value: { kind: "tri_state", value: selected.value },
        });
        displayParts.push(`${input.label}: ${selected.label}`);
        continue;
      }
      const selected = input.options.find((option) => option.value === rawValue);
      if (selected === undefined) {
        nextErrors[input.id] = `${input.label} 항목을 선택해 주세요.`;
        continue;
      }
      values.push({
        input_id: input.id,
        value: { kind: "choice", value: selected.value },
      });
      displayParts.push(`${input.label}: ${selected.label}`);
    }

    setValidationErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      focusFirstError(nextErrors);
      return;
    }
    await onSubmit({
      answer: { mode: "fields", values },
      displayValue: displayParts.join(" · "),
    });
  }

  return (
    <section aria-labelledby={`${baseId}-title`}>
      <Card className="gap-5">
        <CardHeader>
          <CardTitle>
            <h2
              id={`${baseId}-title`}
              ref={headingRef}
              tabIndex={-1}
              className="text-lg font-semibold tracking-tight"
            >
              추가 정보가 필요합니다
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex flex-col gap-1 rounded-lg bg-muted/50 px-4 py-3 text-base leading-7">
            {clarification.prompts.map((prompt) => (
              <p id={promptDomIds.get(prompt.id)} key={prompt.id}>
                {prompt.question}
              </p>
            ))}
          </div>
          <form className="flex flex-col gap-5" onSubmit={handleSubmit} aria-busy={busy} noValidate>
            {clarification.answer_request.mode === "single_text" ? (
              <Field data-invalid={errors[SINGLE_TEXT_KEY] === undefined ? undefined : true}>
                <FieldLabel htmlFor={`${baseId}-single-text`} className="text-base">
                  {clarification.answer_request.label}
                </FieldLabel>
                <Textarea
                  ref={(element) => {
                    if (element !== null) controlRefs.current.set(SINGLE_TEXT_KEY, element);
                  }}
                  id={`${baseId}-single-text`}
                  className="min-h-28 text-base md:text-base"
                  value={singleText}
                  minLength={clarification.answer_request.min_length}
                  maxLength={clarification.answer_request.max_length}
                  rows={4}
                  disabled={busy}
                  aria-invalid={errors[SINGLE_TEXT_KEY] === undefined ? undefined : true}
                  aria-describedby={describedBy(null, SINGLE_TEXT_KEY)}
                  onKeyDown={(event) => {
                    if (composingEnter(event)) event.preventDefault();
                  }}
                  onChange={(event) => {
                    setSingleText(event.target.value);
                    clearInputError(SINGLE_TEXT_KEY);
                  }}
                />
                <CharacterCount
                  length={singleText.length}
                  max={clarification.answer_request.max_length}
                />
                {errors[SINGLE_TEXT_KEY] ? (
                  <p id={errorId(SINGLE_TEXT_KEY)} className="text-sm text-destructive">
                    {errors[SINGLE_TEXT_KEY]}
                  </p>
                ) : null}
              </Field>
            ) : (
              <div className="flex flex-col gap-5">
                {clarification.answer_request.inputs.map((input) => {
                  const promptId = promptDomIds.has(input.prompt_id) ? input.prompt_id : null;
                  if (input.control === "text") {
                    return (
                      <Field
                        key={input.id}
                        data-invalid={errors[input.id] === undefined ? undefined : true}
                      >
                        <FieldLabel htmlFor={`${baseId}-${input.id}`} className="text-base">
                          {input.label}
                        </FieldLabel>
                        <Textarea
                          ref={(element) => {
                            if (element !== null) controlRefs.current.set(input.id, element);
                          }}
                          id={`${baseId}-${input.id}`}
                          className="text-base md:text-base"
                          value={fieldValues[input.id] ?? ""}
                          minLength={input.min_length}
                          maxLength={input.max_length}
                          rows={2}
                          disabled={busy}
                          aria-invalid={errors[input.id] === undefined ? undefined : true}
                          aria-describedby={describedBy(promptId, input.id)}
                          onKeyDown={(event) => {
                            if (composingEnter(event)) event.preventDefault();
                          }}
                          onChange={(event) => {
                            setFieldValues((current) => ({
                              ...current,
                              [input.id]: event.target.value,
                            }));
                            clearInputError(input.id);
                          }}
                        />
                        <CharacterCount
                          length={(fieldValues[input.id] ?? "").length}
                          max={input.max_length}
                        />
                        {errors[input.id] ? (
                          <p id={errorId(input.id)} className="text-sm text-destructive">
                            {errors[input.id]}
                          </p>
                        ) : null}
                      </Field>
                    );
                  }

                  return (
                    // 선택지는 서버가 준 option 그대로 native radio 로 그린다. 기본 선택은 두지 않는다.
                    <FieldSet
                      key={input.id}
                      className="min-w-0 gap-3"
                      disabled={busy}
                      aria-invalid={errors[input.id] === undefined ? undefined : true}
                      aria-describedby={describedBy(promptId, input.id)}
                    >
                      <FieldLegend className="mb-0">{input.label}</FieldLegend>
                      <div className="grid gap-2 sm:grid-cols-[repeat(auto-fit,minmax(10rem,1fr))]">
                        {input.options.map((option, optionIndex) => (
                          <label
                            key={option.value}
                            className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-base transition-colors hover:bg-muted/50 has-checked:border-primary has-checked:bg-primary/5 has-disabled:cursor-not-allowed has-disabled:opacity-50 dark:has-checked:bg-primary/15"
                          >
                            <input
                              ref={(element) => {
                                if (element !== null && optionIndex === 0) {
                                  controlRefs.current.set(input.id, element);
                                }
                              }}
                              className="size-4 shrink-0 accent-primary"
                              type="radio"
                              name={`${baseId}-${input.id}`}
                              value={option.value}
                              checked={(fieldValues[input.id] ?? "") === option.value}
                              onChange={(event) => {
                                setFieldValues((current) => ({
                                  ...current,
                                  [input.id]: event.target.value,
                                }));
                                clearInputError(input.id);
                              }}
                            />
                            <span>{option.label}</span>
                          </label>
                        ))}
                      </div>
                      {errors[input.id] ? (
                        <p id={errorId(input.id)} className="text-sm text-destructive">
                          {errors[input.id]}
                        </p>
                      ) : null}
                    </FieldSet>
                  );
                })}
              </div>
            )}
            {errorEntries.length > 0 || projectedIssues.generalErrors.length > 0 ? (
              <Alert variant="destructive" role="alert">
                <CircleAlertIcon aria-hidden="true" />
                <AlertTitle>입력 내용을 확인해 주세요.</AlertTitle>
                <AlertDescription>
                  <ul className="flex flex-col gap-1">
                    {errorEntries.map(([key, message]) => (
                      <li key={key}>{message}</li>
                    ))}
                    {projectedIssues.generalErrors.map((message) => (
                      <li key={message}>{message}</li>
                    ))}
                  </ul>
                </AlertDescription>
              </Alert>
            ) : null}
            <Button
              type="submit"
              size="lg"
              className="w-full sm:w-auto sm:self-start"
              disabled={busy}
              aria-disabled={busy}
            >
              {busy ? (
                <Spinner data-icon="inline-start" aria-hidden="true" />
              ) : (
                <ArrowRightIcon data-icon="inline-start" aria-hidden="true" />
              )}
              {busy ? "확인 중…" : "입력한 정보로 계속"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </section>
  );
}
