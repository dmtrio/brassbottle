import type { ClassValue } from "clsx"
import { clsx } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// Teach tailwind-merge the admin type scale (src/styles/tokens.css), so a
// `text-body` passed to a component overrides its `text-sm` instead of being
// mistaken for a colour and silently kept alongside it.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["title", "lead", "row-title", "body", "caption", "micro"] }],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
