// the device token's home (phase 7b: the box): keychain in packaged
// builds, localStorage in dev, one-way migration between the eras, and a
// secret-free rev counter carrying the cross-window change signal.

import { describe, expect, it } from "vitest";
import {
  clearSessionIn,
  loadSessionFrom,
  SESSION_REV_KEY,
  storeSessionIn,
  TOKEN_STORAGE_KEY,
  type SecretStore,
} from "./session";

function fakeLocal(seed: Record<string, string> = {}): Storage {
  const map = new Map(Object.entries(seed));
  return {
    get length() {
      return map.size;
    },
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
    key: (i: number) => [...map.keys()][i] ?? null,
  };
}

function fakeSecrets(initial: string | null = null): SecretStore & {
  held: string | null;
} {
  const store = {
    held: initial,
    get: async () => store.held,
    set: async (value: string) => {
      store.held = value;
    },
    clear: async () => {
      store.held = null;
    },
  };
  return store;
}

const broken: SecretStore = {
  get: async () => {
    throw new Error("keychain said no");
  },
  set: async () => {
    throw new Error("keychain said no");
  },
  clear: async () => {
    throw new Error("keychain said no");
  },
};

describe("dev mode (no secret store)", () => {
  it("round-trips through localStorage and bumps the rev", async () => {
    const local = fakeLocal();
    await storeSessionIn(null, local, "tok-1", "dev-9");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBe("tok-1");
    expect(local.getItem(SESSION_REV_KEY)).toBe("1");
    expect(await loadSessionFrom(null, local)).toBe("tok-1");

    await clearSessionIn(null, local);
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(local.getItem("chordial.device_id")).toBeNull();
    expect(local.getItem(SESSION_REV_KEY)).toBe("2");
    expect(await loadSessionFrom(null, local)).toBeNull();
  });
});

describe("packaged mode (keychain)", () => {
  it("reads the keychain, not localStorage", async () => {
    const secrets = fakeSecrets("kc-token");
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "stale-plaintext" });
    expect(await loadSessionFrom(secrets, local)).toBe("kc-token");
  });

  it("stores in the keychain and leaves no localStorage shadow", async () => {
    const secrets = fakeSecrets();
    const local = fakeLocal();
    await storeSessionIn(secrets, local, "tok-2", "dev-1");
    expect(secrets.held).toBe("tok-2");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(local.getItem("chordial.device_id")).toBe("dev-1");
    expect(local.getItem(SESSION_REV_KEY)).toBe("1");
  });

  it("graduates a localStorage-era token into the keychain", async () => {
    const secrets = fakeSecrets();
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "legacy-tok" });
    expect(await loadSessionFrom(secrets, local)).toBe("legacy-tok");
    expect(secrets.held).toBe("legacy-tok");
    // the plaintext copy is GONE, not shadowing the keychain
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("clears both worlds and bumps the rev", async () => {
    const secrets = fakeSecrets("tok-3");
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "tok-3" });
    await clearSessionIn(secrets, local);
    expect(secrets.held).toBeNull();
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(local.getItem(SESSION_REV_KEY)).toBe("1");
  });
});

describe("a keychain that refuses", () => {
  it("falls back to reading localStorage", async () => {
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "fallback-tok" });
    expect(await loadSessionFrom(broken, local)).toBe("fallback-tok");
  });

  it("falls back to storing in localStorage - linking must not brick",
     async () => {
    const local = fakeLocal();
    await storeSessionIn(broken, local, "tok-4", "dev-2");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBe("tok-4");
    expect(local.getItem(SESSION_REV_KEY)).toBe("1");
  });

  it("still clears the localStorage half", async () => {
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "tok-5" });
    await clearSessionIn(broken, local);
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });
});
