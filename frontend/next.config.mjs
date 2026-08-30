/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The container runs `next start`, so every page that reads live data is
  // rendered per request. See the `dynamic` export on the Server Component
  // pages: without it `next build` tries to prerender them and fails, because
  // the API is not running during the image build.
};

export default nextConfig;
