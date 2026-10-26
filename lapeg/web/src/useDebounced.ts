import { useEffect, useState } from "react";

/** The value, updated only after it stops changing for `ms`. Compared by content, so an equal value
 * passed again (a new object with the same fields) does not trigger a new request. */
export function useDebounced<T>(value: T, ms = 160): T {
  const [v, setV] = useState(value);
  const key = JSON.stringify(value);
  useEffect(() => {
    const t = setTimeout(() => setV((old) => (JSON.stringify(old) === key ? old : (JSON.parse(key) as T))), ms);
    return () => clearTimeout(t);
  }, [key, ms]);
  return v;
}
