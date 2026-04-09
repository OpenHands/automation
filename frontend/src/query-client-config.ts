import { QueryCache, MutationCache, QueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { displayErrorToast } from "./utils/custom-toast-handlers";

const retrieveErrorMessage = (error: unknown): string | null => {
  if (error instanceof AxiosError) {
    const data = error.response?.data;
    if (typeof data === "string") return data;
    if (data && typeof data === "object" && "detail" in data) {
      return String(data.detail);
    }
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return null;
};

const handle401Error = (error: unknown, qc: QueryClient) => {
  if (error instanceof AxiosError) {
    if (error.response?.status === 401 || error.status === 401) {
      qc.invalidateQueries({ queryKey: ["user", "authenticated"] });
    }
  }
};

const shownErrors = new Set<string>();

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      const isAuthQuery =
        query.queryKey[0] === "user" && query.queryKey[1] === "authenticated";
      if (!isAuthQuery) {
        handle401Error(error, queryClient);
      }

      if (!query.meta?.disableToast) {
        const errorMessage = retrieveErrorMessage(error);

        const key = errorMessage || "generic";
        if (!shownErrors.has(key)) {
          displayErrorToast(errorMessage);
          shownErrors.add(key);

          setTimeout(() => {
            shownErrors.delete(key);
          }, 3000);
        }
      }
    },
  }),
  mutationCache: new MutationCache({
    onError: (error, _, __, mutation) => {
      handle401Error(error, queryClient);

      if (!mutation?.meta?.disableToast) {
        const message = retrieveErrorMessage(error);
        displayErrorToast(message);
      }
    },
  }),
});
