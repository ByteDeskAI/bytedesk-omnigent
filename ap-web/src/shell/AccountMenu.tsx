/**
 * Account/operator footer for the sidebar.
 *
 * In accounts mode, renders only after the ``/v1/info`` capabilities
 * probe reports accounts support and ``/auth/me`` resolves. In non-accounts
 * mode, renders only when the standalone Omni CLI terminal is enabled.
 * Inside, shows:
 *
 * - The signed-in username, with an "Admin" badge when applicable.
 * - A link to ``/members`` (only for admins).
 * - A sign-out item that clears the session cookie via
 *   ``POST /auth/logout`` and hard-navigates back to ``/login``.
 *
 * Sits at the bottom of the left sidebar as a full-width row in its
 * own bordered footer block, with the dropdown opening upward. It
 * owns that footer chrome on purpose — the whole thing (border +
 * padding included) disappears when the component gates out.
 */

import { useCallback, useEffect, useState } from "react";
import { changePassword, type CurrentAccount, getMe, logout } from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { AccountChangePasswordDialog } from "./components/account-menu/AccountChangePasswordDialog";
import { AccountsMenuDropdown } from "./components/account-menu/AccountsMenuDropdown";
import { LocalOperatorMenu } from "./components/account-menu/LocalOperatorMenu";

export function AccountMenu() {
  const info = useServerInfo();
  if (info === "loading") return null;
  const accountsEnabled = info.accounts_enabled;
  const terminalEnabled = info.omni_cli_terminal_enabled;

  const [me, setMe] = useState<CurrentAccount | null | "unknown">("unknown");

  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  useEffect(() => {
    if (!accountsEnabled) return;
    void (async () => {
      const account = await getMe();
      setMe(account);
    })();
  }, [accountsEnabled]);

  const onSignOut = useCallback(async () => {
    await logout();
    window.location.href = "/login";
  }, []);

  const resetPwForm = useCallback(() => {
    setOldPw("");
    setNewPw("");
    setConfirmPw("");
    setPwError(null);
    setPwDone(false);
    setPwBusy(false);
  }, []);

  const onSubmitPassword = useCallback(async () => {
    if (newPw !== confirmPw) {
      setPwError("New passwords don't match.");
      return;
    }
    setPwBusy(true);
    setPwError(null);
    const result = await changePassword({ old_password: oldPw, new_password: newPw });
    setPwBusy(false);
    if (result.ok) {
      setPwDone(true);
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
    } else {
      setPwError(result.error);
    }
  }, [oldPw, newPw, confirmPw]);

  if (!accountsEnabled) {
    return <LocalOperatorMenu terminalEnabled={terminalEnabled} />;
  }
  if (me === "unknown") return null;
  if (me === null) return null;

  return (
    <>
      <AccountsMenuDropdown
        me={me}
        terminalEnabled={terminalEnabled}
        onOpenChangePassword={() => {
          resetPwForm();
          setPwOpen(true);
        }}
        onSignOut={() => void onSignOut()}
      />
      <AccountChangePasswordDialog
        open={pwOpen}
        onOpenChange={(open) => {
          setPwOpen(open);
          if (!open) resetPwForm();
        }}
        oldPw={oldPw}
        newPw={newPw}
        confirmPw={confirmPw}
        busy={pwBusy}
        error={pwError}
        done={pwDone}
        onOldPwChange={setOldPw}
        onNewPwChange={setNewPw}
        onConfirmPwChange={setConfirmPw}
        onSubmit={() => void onSubmitPassword()}
      />
    </>
  );
}