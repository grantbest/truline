import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Variant = "default" | "ghost" | "outline" | "danger";
type Size = "sm" | "md";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

const variantClasses: Record<Variant, string> = {
  default:
    "bg-accent-muted/30 hover:bg-accent-muted/50 text-accent border border-accent-muted/40",
  ghost: "hover:bg-bg-hover text-fg-muted hover:text-fg",
  outline: "border border-border-strong hover:bg-bg-hover text-fg",
  danger: "border border-neg/30 text-neg hover:bg-neg/10",
};

const sizeClasses: Record<Size, string> = {
  sm: "h-9 md:h-7 px-3 md:px-2 text-sm md:text-xs",
  md: "h-11 md:h-8 px-4 md:px-3 text-base md:text-sm",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", size = "md", ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded font-medium transition-colors",
        "disabled:opacity-40 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent",
        variantClasses[variant],
        sizeClasses[size],
        className,
      )}
      {...props}
    />
  ),
);
Button.displayName = "Button";
