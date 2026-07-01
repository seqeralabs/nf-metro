import type { APIRoute, GetStaticPaths } from "astro";
import { getCollection, type CollectionEntry } from "astro:content";
import { renderOgImage, pngResponse } from "../../../lib/og-image.mjs";

export const getStaticPaths = (async () => {
  const entries = await getCollection("pipelines");
  return entries.map((entry) => ({
    params: { entry: entry.id },
    props: { entry },
  }));
}) satisfies GetStaticPaths;

export const GET: APIRoute<{ entry: CollectionEntry<"pipelines"> }> = async ({
  props,
}) => {
  const { title, description, src } = props.entry.data;
  const png = await renderOgImage({
    kicker: "nf-core pipeline",
    title,
    subtitle: description,
    mmdPath: src,
  });
  return pngResponse(png);
};
