// Fetch layer. Two base URLs, deliberately, because the same code runs in two
// places with different networking:
//
//   Server Components run INSIDE the web container, where the API is reachable
//   as the compose service name `api`. localhost there is the web container
//   itself, which serves nothing on 8000.
//
//   Client Components run in the user's BROWSER, which cannot resolve `api` at
//   all and has to use the port published on the host.
//
// Getting this wrong produces the classic symptom: analytics and candidate
// detail render fine while the dashboard is permanently empty, or vice versa.

export const SERVER_API_BASE =
  process.env.API_URL_INTERNAL ?? "http://api:8000/api/v1";

export const BROWSER_API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

/** An error carrying what the API's {error:{code,message}} envelope said, so
 *  the UI can show a real sentence instead of "something went wrong". */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code = "http_error";
  try {
    const body = await res.json();
    if (body?.error?.message) {
      message = body.error.message;
      code = body.error.code ?? code;
    }
  } catch {
    // A non-JSON body (a proxy error page, say) leaves the status line as the
    // message, which is still more useful than a blank screen.
  }
  return new ApiError(message, res.status, code);
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${base}${path}`, { cache: "no-store", ...init });
  } catch (cause) {
    // A network-level failure (API down, DNS, CORS block) never reaches the
    // status check, and its native message is "Failed to fetch". Name the
    // likely cause instead, since this is the error a reviewer sees first if
    // the stack is only half up.
    throw new ApiError(
      `Could not reach the API at ${base}. Is the api service running?`,
      0,
      "network_error",
    );
  }
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Server Components only — uses the in-container base URL. */
export function serverFetch<T>(path: string): Promise<T> {
  return request<T>(SERVER_API_BASE, path);
}

/** Browser only. */
export function apiGet<T>(path: string): Promise<T> {
  return request<T>(BROWSER_API_BASE, path);
}

/**
 * Browser mutations. `actor` becomes the X-Actor header, which is the whole of
 * this app's identity model: there is no login, and the header is what the
 * audit log records as having made the change.
 */
export function apiMutate<T>(
  path: string,
  method: "POST" | "PATCH",
  actor: string,
  body?: unknown,
): Promise<T> {
  return request<T>(BROWSER_API_BASE, path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Actor": actor,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "An unexpected error occurred.";
}
