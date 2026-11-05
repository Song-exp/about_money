import type { ProfileSettings, UserProfileLocal, StoreCategory } from "./types";

export const EMPTY_PROFILE_SETTINGS: ProfileSettings = {
  gender: null, cardIds: [], telecom: null, payMethods: [],
  studentCredentials: [], benefitPrograms: [],
};

/** Display only the user's own settings; old demo identifiers are ignored. */
export function getPersonaForProfile(profile: UserProfileLocal | null) {
  const settings = profile?.personalProfile ?? profile ?? EMPTY_PROFILE_SETTINGS;
  return {
    id: "me", name: "나", gender: settings.gender,
    cardLabel: settings.cardIds.length > 0 ? `내 카드 ${settings.cardIds.length}개` : "선택 카드 없음",
    preferredCategories: ["cafe", "restaurant", "convenience"] as StoreCategory[],
  };
}
