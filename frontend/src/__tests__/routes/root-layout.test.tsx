import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { AxiosError, AxiosHeaders } from "axios";
import OpenHandsService from "#/api/openhands-service";
import RootLayout from "#/routes/root-layout";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function renderLayout(queryClient?: QueryClient) {
  const qc = queryClient ?? createTestQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RootLayout />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockUser = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets"],
};

const error401 = new AxiosError(
  "Unauthorized",
  "ERR_BAD_REQUEST",
  undefined,
  undefined,
  {
    status: 401,
    data: { error: "User is not authenticated" },
    statusText: "Unauthorized",
    headers: {},
    config: { headers: new AxiosHeaders() },
  },
);

describe("RootLayout", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("renders outlet content when authenticated", async () => {
    vi.spyOn(OpenHandsService, "authenticate").mockResolvedValue(true);
    vi.spyOn(OpenHandsService, "getMe").mockResolvedValue(mockUser);
    renderLayout();

    await waitFor(() => {
      expect(screen.getByRole("main")).toBeInTheDocument();
    });
  });

  it("redirects to login when unauthenticated and no login method stored", async () => {
    const hrefSetter = vi.fn();
    Object.defineProperty(window, "location", {
      value: {
        ...window.location,
        pathname: "/automations",
        get href() {
          return "http://localhost/automations";
        },
        set href(url: string) {
          hrefSetter(url);
        },
      },
      writable: true,
      configurable: true,
    });

    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(error401);
    renderLayout();

    await waitFor(() => {
      expect(hrefSetter).toHaveBeenCalledWith("/login?redirect=%2Fautomations");
    });
  });

  it("shows reauth modal when unauthenticated and login method is stored", async () => {
    localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, "github");

    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(error401);
    renderLayout();

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByText("AUTH$LOGGING_BACK_IN")).toBeInTheDocument();
    });
  });

  it("redirects to login when login method is removed via cross-tab storage event", async () => {
    const hrefSetter = vi.fn();
    Object.defineProperty(window, "location", {
      value: {
        ...window.location,
        pathname: "/automations",
        get href() {
          return "http://localhost/automations";
        },
        set href(url: string) {
          hrefSetter(url);
        },
      },
      writable: true,
      configurable: true,
    });

    localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, "github");

    vi.spyOn(OpenHandsService, "authenticate").mockResolvedValue(true);
    vi.spyOn(OpenHandsService, "getMe").mockResolvedValue(mockUser);

    renderLayout();

    await waitFor(() => {
      expect(screen.getByRole("main")).toBeInTheDocument();
    });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.LOGIN_METHOD,
          newValue: null,
          oldValue: "github",
        }),
      );
    });

    await waitFor(() => {
      expect(hrefSetter).toHaveBeenCalledWith("/login?redirect=%2Fautomations");
    });
  });

  it("shows reauth modal on session expiry when login method exists", async () => {
    localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, "github");

    const authenticateSpy = vi
      .spyOn(OpenHandsService, "authenticate")
      .mockResolvedValue(true);
    vi.spyOn(OpenHandsService, "getMe").mockResolvedValue(mockUser);

    const queryClient = createTestQueryClient();
    renderLayout(queryClient);

    await waitFor(() => {
      expect(screen.getByRole("main")).toBeInTheDocument();
    });

    authenticateSpy.mockRejectedValue(error401);
    queryClient.setQueryData(["user", "authenticated"], false);

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      expect(screen.getByText("AUTH$LOGGING_BACK_IN")).toBeInTheDocument();
    });
  });
});
