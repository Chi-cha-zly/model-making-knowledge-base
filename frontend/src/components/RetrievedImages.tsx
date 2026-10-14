type ImageReference = { title: string; attachment_ids?: number[] }

export default function RetrievedImages({
  citation,
}: {
  citation: ImageReference
}) {
  const ids = [...new Set(citation.attachment_ids ?? [])]
    .filter((id) => Number.isSafeInteger(id) && id > 0)
    .slice(0, 6)
  if (!ids.length) return null
  return (
    <section
      className="citation-images"
      aria-label={`${citation.title}的原始图片`}
    >
      <p>检索到的原始图片</p>
      <div>
        {ids.map((id, index) => (
          <a
            key={id}
            href={`/api/v1/attachments/${id}`}
            target="_blank"
            rel="noreferrer"
          >
            <img
              src={`/api/v1/attachments/${id}`}
              loading="lazy"
              alt={`${citation.title} · 原始图片 ${index + 1}`}
            />
          </a>
        ))}
      </div>
    </section>
  )
}
