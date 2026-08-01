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

import type {
  AgentValidationIssue,
  ClarificationView,
  StructuredClarificationAnswer,
} from "@/contracts/agent";

import styles from "./assistant-workspace.module.css";

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
    <section className={styles.clarificationCard} aria-labelledby={`${baseId}-title`}>
      <p className={styles.eyebrow}>확인을 위한 추가 질문</p>
      <h2 id={`${baseId}-title`} ref={headingRef} tabIndex={-1}>
        추가 정보가 필요합니다
      </h2>
      <div className={styles.promptList}>
        {clarification.prompts.map((prompt) => (
          <p id={promptDomIds.get(prompt.id)} key={prompt.id}>
            {prompt.question}
          </p>
        ))}
      </div>
      <form onSubmit={handleSubmit} aria-busy={busy} noValidate>
        {clarification.answer_request.mode === "single_text" ? (
          <div className={styles.freeTextGroup}>
            <label htmlFor={`${baseId}-single-text`}>
              {clarification.answer_request.label}
            </label>
            <textarea
              ref={(element) => {
                if (element !== null) controlRefs.current.set(SINGLE_TEXT_KEY, element);
              }}
              id={`${baseId}-single-text`}
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
            <small>
              {singleText.length.toLocaleString("ko-KR")} /{" "}
              {clarification.answer_request.max_length.toLocaleString("ko-KR")}자
            </small>
            {errors[SINGLE_TEXT_KEY] ? (
              <p id={errorId(SINGLE_TEXT_KEY)} className={styles.inlineError}>
                {errors[SINGLE_TEXT_KEY]}
              </p>
            ) : null}
          </div>
        ) : (
          <div className={styles.structuredFields}>
            {clarification.answer_request.inputs.map((input) => {
              const promptId = promptDomIds.has(input.prompt_id) ? input.prompt_id : null;
              if (input.control === "text") {
                return (
                  <div key={input.id} className={styles.structuredTextField}>
                    <label htmlFor={`${baseId}-${input.id}`}>{input.label}</label>
                    <textarea
                      ref={(element) => {
                        if (element !== null) controlRefs.current.set(input.id, element);
                      }}
                      id={`${baseId}-${input.id}`}
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
                    <small>
                      {(fieldValues[input.id] ?? "").length.toLocaleString("ko-KR")} /{" "}
                      {input.max_length.toLocaleString("ko-KR")}자
                    </small>
                    {errors[input.id] ? (
                      <p id={errorId(input.id)} className={styles.inlineError}>
                        {errors[input.id]}
                      </p>
                    ) : null}
                  </div>
                );
              }

              return (
                <fieldset
                  key={input.id}
                  className={styles.choiceGroup}
                  disabled={busy}
                  aria-invalid={errors[input.id] === undefined ? undefined : true}
                  aria-describedby={describedBy(promptId, input.id)}
                >
                  <legend>{input.label}</legend>
                  {input.options.map((option, optionIndex) => (
                    <label key={option.value} className={styles.choiceOption}>
                      <input
                        ref={(element) => {
                          if (element !== null && optionIndex === 0) {
                            controlRefs.current.set(input.id, element);
                          }
                        }}
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
                  {errors[input.id] ? (
                    <p id={errorId(input.id)} className={styles.inlineError}>
                      {errors[input.id]}
                    </p>
                  ) : null}
                </fieldset>
              );
            })}
          </div>
        )}
        {errorEntries.length > 0 || projectedIssues.generalErrors.length > 0 ? (
          <div className={styles.validationSummary} role="alert">
            <p>입력 내용을 확인해 주세요.</p>
            {errorEntries.map(([key, message]) => (
              <p key={key}>{message}</p>
            ))}
            {projectedIssues.generalErrors.map((message) => (
              <p key={message}>{message}</p>
            ))}
          </div>
        ) : null}
        <button
          type="submit"
          className={styles.primaryButton}
          disabled={busy}
          aria-disabled={busy}
        >
          {busy ? "확인 중…" : "입력한 정보로 계속"}
        </button>
      </form>
    </section>
  );
}
