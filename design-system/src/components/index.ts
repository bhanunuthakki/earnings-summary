/**
 * Public components barrel — kit-core (Phase 2). Merges the three
 * Phase-2 sibling agents' work (buttons/pills/chips/wells/dots, ticker
 * label/menu/toolbar, form controls) into one export surface.
 */
export * from "./Button";
export * from "./Pill";
export * from "./Chip";
export * from "./Well";
export * from "./Dot";
export * from "./NumText";
export * from "../lib/tone";

export * from "./TickerLabel";
export * from "./Label";
export * from "./Menu";
export * from "./Toolbar";

export { Input, type InputProps, type KitInputType } from "./Input";
export { Textarea, type TextareaProps } from "./Textarea";
export { Select, type SelectProps, type SelectOption } from "./Select";
export { MultiSelect, type MultiSelectProps, type MultiSelectOption } from "./MultiSelect";
export { DateField, type DateFieldProps } from "./DateField";
