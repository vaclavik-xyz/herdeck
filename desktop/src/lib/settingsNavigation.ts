export interface SettingsNavItem<TIcon = string> {
  key: string;
  icon: TIcon;
  label: string;
}

export interface SettingsNavGroup<TIcon = string> {
  label: string;
  items: SettingsNavItem<TIcon>[];
}

/** Filter section navigation by both its visible title and user-facing purpose. */
export function filterSettingsNavigation<TIcon>(
  groups: SettingsNavGroup<TIcon>[],
  descriptions: Record<string, string>,
  query: string,
  language: string,
): SettingsNavGroup<TIcon>[] {
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
