import { useEffect, useState } from "react";
import { getMe } from "@/lib/accountsApi";
import { useNavigate } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";

export function useConnectorAdminAccess() {
  const navigate = useNavigate();
  const info = useServerInfo();
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    if (info === "loading") return;
    if (!info.accounts_enabled) {
      setAllowed(true);
      return;
    }
    void (async () => {
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setAllowed(me.is_admin);
    })();
  }, [info, navigate]);

  return allowed;
}