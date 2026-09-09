/// <reference types="vite/client" />

// Pulls in Vite's ambient module declarations — notably the `?raw` suffix used
// by src/test/collectionGlobs.test.ts to read the collection configs as text.
// The app has no @types/node, so node:fs is not an option there.
