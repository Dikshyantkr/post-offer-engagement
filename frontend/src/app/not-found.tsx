import Link from "next/link";

export default function NotFound() {
  return (
    <div className="rounded border border-slate-300 bg-white p-6">
      <h1 className="text-lg font-semibold">Not found</h1>
      <p className="mt-2 text-sm text-slate-600">
        That page or record does not exist.
      </p>
      <Link href="/" className="mt-4 inline-block text-sm text-blue-700 underline">
        Back to the dashboard
      </Link>
    </div>
  );
}
