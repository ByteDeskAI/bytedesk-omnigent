import { useEffect, useState } from "react";
import { readNavigatorOnline, subscribeOnlineStatus } from "@/lib/pwa/offline";

export function useOffline(): boolean {
  const [online, setOnline] = useState(readNavigatorOnline);
  useEffect(() => subscribeOnlineStatus(setOnline), []);
  return !online;
}