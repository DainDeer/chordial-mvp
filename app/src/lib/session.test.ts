// the device token's home (phase 7b: the box): keychain in packaged
// builds, localStorage in dev, one-way migration between the eras, and a
// secret-free rev counter carrying the cross-window change signal.

import { describe, expect, it } from "vitest";
import {
  CLEAR_PENDING_KEY,
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
  it("reads the keychain when localStorage has nothing", async () => {
    const secrets = fakeSecrets("kc-token");
    const local = fakeLocal();
    expect(await loadSessionFrom(secrets, local)).toBe("kc-token");
  });

  it("a localStorage token BEATS the keychain value and moves home",
     async () => {
    // both existing means the keychain missed a newer write (the
    // fallback path) - the keychain's copy is the older, staler one
    const secrets = fakeSecrets("older-kc-token");
    const local = fakeLocal({ [TOKEN_STORAGE_KEY]: "newer-local-token" });
    expect(await loadSessionFrom(secrets, local)).toBe("newer-local-token");
    expect(secrets.held).toBe("newer-local-token");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
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

describe("a flaky keychain never resurrects a revoked token (sol 7b)", () => {
  it("the full repro: failed clear + failed relink set, then it heals",
     async () => {
    // the keychain holds token A; the device gets revoked while the
    // keychain is refusing everything
    const secrets = fakeSecrets("revoked-A");
    let refusing = true;
    const flaky: SecretStore = {
      get: async () => {
        if (refusing) throw new Error("no");
        return secrets.get();
      },
      set: async (v) => {
        if (refusing) throw new Error("no");
        return secrets.set(v);
      },
      clear: async () => {
        if (refusing) throw new Error("no");
        return secrets.clear();
      },
    };
    const local = fakeLocal();

    await clearSessionIn(flaky, local); // revocation: clear refused
    expect(local.getItem(CLEAR_PENDING_KEY)).not.toBeNull();

    // relink: the set is refused too, token B falls back to localStorage
    await storeSessionIn(flaky, local, "fresh-B", "dev-1");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBe("fresh-B");
    expect(secrets.held).toBe("revoked-A"); // the ghost is still in there

    // next launch, keychain healthy again: B wins, A dies, B moves home
    refusing = false;
    expect(await loadSessionFrom(flaky, local)).toBe("fresh-B");
    expect(secrets.held).toBe("fresh-B");
    expect(local.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(local.getItem(CLEAR_PENDING_KEY)).toBeNull();
  });

  it("a pending clear hides the stuck value even before relinking",
     async () => {
    const secrets = fakeSecrets("revoked-A");
    const local = fakeLocal({ [CLEAR_PENDING_KEY]: "1" });
    // keychain works now: the owed clear lands first, nothing is served
    expect(await loadSessionFrom(secrets, local)).toBeNull();
    expect(secrets.held).toBeNull();
    expect(local.getItem(CLEAR_PENDING_KEY)).toBeNull();
  });

  it("a still-broken keychain with a pending clear serves null, not the ghost",
     async () => {
    const local = fakeLocal({ [CLEAR_PENDING_KEY]: "1" });
    expect(await loadSessionFrom(broken, local)).toBeNull();
    expect(local.getItem(CLEAR_PENDING_KEY)).not.toBeNull(); // still owed
  });

  it("a successful relink set supersedes the owed clear", async () => {
    const secrets = fakeSecrets("revoked-A");
    const local = fakeLocal({ [CLEAR_PENDING_KEY]: "1" });
    await storeSessionIn(secrets, local, "fresh-B", "dev-1");
    expect(secrets.held).toBe("fresh-B");
    expect(local.getItem(CLEAR_PENDING_KEY)).toBeNull();
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
