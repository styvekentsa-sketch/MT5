import { useCallback, useEffect, useState } from "react";

/** Événement `beforeinstallprompt` (Chrome, Edge, Samsung Internet) : non typé dans lib.dom. */
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true // Safari iOS
  );
}

function isIos(): boolean {
  const ua = navigator.userAgent;
  // iPadOS se présente comme un Mac : on le reconnaît à son écran tactile
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

/**
 * Installation de la PWA :
 * - Android / PC (Chrome, Edge, Samsung Internet) : invite native via `install()`
 * - iOS / iPadOS : aucune API, l'utilisateur passe par Partager > « Sur l'écran d'accueil »
 */
export function useInstallPrompt() {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(isStandalone);

  useEffect(() => {
    const onPrompt = (event: Event) => {
      event.preventDefault(); // on affiche notre propre bouton « Installer »
      setDeferred(event as BeforeInstallPromptEvent);
    };
    const onInstalled = () => {
      setInstalled(true);
      setDeferred(null);
    };
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  const install = useCallback(async () => {
    if (!deferred) return false;
    await deferred.prompt();
    const { outcome } = await deferred.userChoice;
    setDeferred(null);
    return outcome === "accepted";
  }, [deferred]);

  return {
    canInstall: deferred !== null && !installed,
    installed,
    ios: isIos(),
    install,
  };
}

/** État réseau de l'appareil (événements online / offline). */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() => navigator.onLine);
  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  return online;
}
