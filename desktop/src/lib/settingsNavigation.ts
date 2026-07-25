export interface SettingsNavItem {
  key: string;
  icon: string;
  label: string;
}

export interface SettingsNavGroup {
  label: string;
  items: SettingsNavItem[];
}

/** Filter section navigation by both its visible title and user-facing purpose. */
export function filterSettingsNavigation(
  groups: SettingsNavGroup[],
  descriptions: Record<string, string>,
  query: string,
  language: string,
): SettingsNavGroup[] {
  const needle = query.trim().toLocaleLowerCase(language);
  if (!needle) return groups;
  return groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) =>
        `${item.label} ${descriptions[item.key] ?? ""}`.toLocaleLowerCase(language).includes(needle)
      ),
    }))
    .filter((group) => group.items.length > 0);
}
