import type { Readable } from "svelte/store";

export type FieldValidationMessages = Readable<Record<string, string[]>>;

export const FIELD_VALIDATION_CONTEXT = Symbol("herdeck-field-validation");
