import { useEffect, useState } from "react";

/** Minimal hash router: "#/subjects/cust-007" -> ["subjects", "cust-007"]. */
export function useRoute(): string[] {
  const parse = () =>
    window.location.hash
      .replace(/^#\/?/, "")
      .split("/")
      .filter(Boolean)
      .map(decodeURIComponent);
  const [route, setRoute] = useState<string[]>(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function navigate(...parts: string[]): void {
  window.location.hash = "/" + parts.map(encodeURIComponent).join("/");
}
