import {
  forwardRef,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

const baseField =
  "h-10 md:h-8 rounded border border-border bg-bg-subtle px-2.5 md:px-2 text-base md:text-sm text-fg placeholder:text-fg-subtle focus:outline-none focus:border-accent-muted focus:ring-1 focus:ring-accent-muted/50";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input ref={ref} className={cn(baseField, className)} {...props} />
  ),
);
Input.displayName = "Input";

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => (
    <select ref={ref} className={cn(baseField, "pr-7", className)} {...props} />
  ),
);
Select.displayName = "Select";

// Same field styling as Input, minus the fixed height — notes are markdown
// bodies and want room to breathe.
export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(baseField, "h-auto md:h-auto py-2 leading-relaxed resize-y", className)}
    {...props}
  />
));
Textarea.displayName = "Textarea";
