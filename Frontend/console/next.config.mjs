/**
 * One build, two homes.
 *
 * `basePath: '/console'` is what lets a single export serve both targets: the
 * Azure container mounts it at /console (same origin as the API), and Firebase
 * Hosting publishes it under /console too. Without a shared path the two
 * deployments would need different builds, because Next bakes asset URLs in.
 *
 * `output: 'export'` keeps it a folder of files — no Node runtime beside the
 * API, and nothing to keep alive.
 */
const nextConfig = {
  output: 'export',
  basePath: '/console',
  trailingSlash: true,
  reactStrictMode: true,
  images: { unoptimized: true },
};

export default nextConfig;
