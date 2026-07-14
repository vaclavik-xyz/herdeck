<script lang="ts">
  import { getAt, type ConfigPayload } from "../configClient";
  import NotificationsSection from "./NotificationsSection.svelte";

  let { initial, editProfile = null }: { initial: ConfigPayload; editProfile?: string | null } = $props();
  let payload = $state(initial);

  const allowedUsers = $derived(
    (getAt(payload, "base", "notifications", "telegram") as Record<string, unknown> | undefined)
      ?.allowed_user_ids,
  );
  const profileTelegram = $derived(
    editProfile == null
      ? undefined
      : ((payload.profiles[editProfile]?.notifications as Record<string, unknown> | undefined)
          ?.telegram as Record<string, unknown> | undefined),
  );
</script>

<NotificationsSection bind:payload {editProfile} onChange={() => {}} onError={() => {}} />
<output class="allowed-payload">{JSON.stringify(allowedUsers)}</output>
<output class="profile-telegram">{JSON.stringify(profileTelegram)}</output>
