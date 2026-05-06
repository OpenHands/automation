import type { Config } from "@react-router/dev/config";

const unpackClientDirectory = async () => {
  const fs = await import("fs");
  const path = await import("path");

  const buildDir = path.resolve(__dirname, "build");
  const clientDir = path.resolve(buildDir, "client");

  const files = await fs.promises.readdir(clientDir);
  await Promise.all(
    files.map((file) =>
      fs.promises.rename(
        path.resolve(clientDir, file),
        path.resolve(buildDir, file),
      ),
    ),
  );

  await fs.promises.rmdir(clientDir);
};

export default {
  appDirectory: "src",
  basename: "/automations/",
  buildEnd: unpackClientDirectory,
  ssr: false,
} satisfies Config;
