import { create } from "zustand";
import { devtools } from "zustand/middleware";
import type { User } from "#/types/user";

interface UserState {
  user: User | null;
  isInitialized: boolean;
  setUser: (user: User) => void;
  clearUser: () => void;
}

export const useUserStore = create<UserState>()(
  devtools(
    (set) => ({
      user: null,
      isInitialized: false,
      setUser: (user) => set({ user, isInitialized: true }),
      clearUser: () => set({ user: null, isInitialized: false }),
    }),
    { name: "UserStore" },
  ),
);
