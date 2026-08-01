"use client";

import type { ComponentPropsWithRef, MouseEvent } from "react";

import { cx } from "@/lib/class-names";

import styles from "./button.module.css";

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";

export type ButtonProps = ComponentPropsWithRef<"button"> & {
  fullWidth?: boolean;
  isPending?: boolean;
  variant?: ButtonVariant;
};

export function Button({
  "aria-disabled": ariaDisabled,
  "aria-busy": ariaBusy,
  children,
  className,
  fullWidth = false,
  isPending = false,
  onClick,
  type = "button",
  variant = "primary",
  ...props
}: ButtonProps) {
  const isAriaDisabled = isPending || ariaDisabled === true || ariaDisabled === "true";

  function handleClick(event: MouseEvent<HTMLButtonElement>) {
    if (isAriaDisabled) {
      event.preventDefault();
      return;
    }

    onClick?.(event);
  }

  return (
    <button
      {...props}
      aria-busy={isPending || ariaBusy || undefined}
      aria-disabled={isAriaDisabled || undefined}
      className={cx(styles.button, styles[variant], fullWidth && styles.fullWidth, className)}
      data-pending={isPending ? "true" : undefined}
      data-ui="button"
      onClick={handleClick}
      type={type}
    >
      {children}
    </button>
  );
}
