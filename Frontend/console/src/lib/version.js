/**
 * Published version numbers for the console and the documentation.
 *
 * Two numbers, not one: the console UI and the API documentation change on
 * different schedules and are read by different people. A customer citing a
 * documentation version in a support request should not have that number move
 * because a button was restyled.
 *
 * Neither tracks the image tag. `/healthz` reports the build (`dev-<sha>`),
 * which is what an operator needs; these are what a reader cites.
 *
 * Bump by hand, in this file only:
 *   CONSOLE_VERSION — a change to the console UI
 *   DOCS_VERSION    — a change to anything under content/docs/
 */
export const CONSOLE_VERSION = 'V1.0.2';
export const DOCS_VERSION = 'V1.0.6';
