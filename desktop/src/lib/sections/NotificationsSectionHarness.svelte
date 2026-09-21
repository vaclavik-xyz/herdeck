<script lang="ts">
  import { getAt, type ConfigPayload } from "../configClient";
  import NotificationsSection from "./NotificationsSection.svelte";

  let { initial, editProfile = null }: { initial: ConfigPayload; editProfile?: string | null } = $props();
  function initialPayload(): ConfigPayload { return initial; }
  let payload = $state(initialPayload());

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
  const profileSounds = $derived(
    editProfile == null
      ? undefined
      : ((payload.profiles[editProfile]?.notifications as Record<string, unknown> | undefined)
          ?.sounds as Record<string, unknown> | undefined),
  );
  const baseSounds = $derived(
    (getAt(payload, "base", "notifications", "sounds") as Record<string, unknown> | undefined) ??
      undefined,
  );
</script>

<NotificationsSection bind:payload {editProfile} onChange={() => {}} onError={() => {}} />
<output class="allowed-payload">{JSON.stringify(allowedUsers)}</output>
<output class="profile-telegram">{JSON.stringify(profileTelegram)}</output>
<output class="profile-sounds">{JSON.stringify(profileSounds)}</output>
<output class="sounds-payload">{JSON.stringify(baseSounds)}</output>
