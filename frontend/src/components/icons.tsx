import type { ReactNode } from "react";

interface IconProps {
  size?: number;
  className?: string;
}

function Shell({
  size = 16,
  className,
  children,
  strokeWidth = 1.7,
}: IconProps & { children: ReactNode; strokeWidth?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      {children}
    </svg>
  );
}

export function BridgeMark(props: IconProps) {
  return (
    <Shell {...props} strokeWidth={1.6}>
      <path d="M3 17.5h18" />
      <path d="M6.6 17.5V7M17.4 17.5V7" />
      <path d="M6.6 8.6c1.8 3.6 3.6 5 5.4 5s3.6-1.4 5.4-5" />
      <path d="M9.6 10.4v7.1M14.4 10.4v7.1" />
    </Shell>
  );
}

export function SparkIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="M12 3.5 13.6 9 19 10.5 13.6 12 12 17.5 10.4 12 5 10.5 10.4 9 12 3.5Z" />
      <path d="M18.5 15.5v5M16 18h5" />
    </Shell>
  );
}

export function DownloadIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="M12 4v10.5" />
      <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
      <path d="M5 20h14" />
    </Shell>
  );
}

export function ZoomInIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <circle cx="11" cy="11" r="6.2" />
      <path d="m15.6 15.6 4 4" />
      <path d="M8.8 11h4.4M11 8.8v4.4" />
    </Shell>
  );
}

export function ZoomOutIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <circle cx="11" cy="11" r="6.2" />
      <path d="m15.6 15.6 4 4" />
      <path d="M8.8 11h4.4" />
    </Shell>
  );
}

export function ResetIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5" />
    </Shell>
  );
}

export function DatabaseIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <ellipse cx="12" cy="6" rx="7" ry="3" />
      <path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
      <path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" />
    </Shell>
  );
}

export function ImageIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <circle cx="8.5" cy="9.5" r="1.5" />
      <path d="m4.5 17 5-5 3.5 3.5 2.5-2.5 4 4" />
    </Shell>
  );
}

export function AlertIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.8v5" />
      <path d="M12 16.2h.01" />
    </Shell>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    </Shell>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="M6 6l12 12M18 6 6 18" />
    </Shell>
  );
}

export function CodeIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="m8.5 8-4.5 4 4.5 4M15.5 8l4.5 4-4.5 4" />
    </Shell>
  );
}

export function ArrowRightIcon(props: IconProps) {
  return (
    <Shell {...props}>
      <path d="M4 12h15" />
      <path d="m13.5 6.5 5.5 5.5-5.5 5.5" />
    </Shell>
  );
}
