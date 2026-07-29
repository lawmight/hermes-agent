const EMOJI_RE = /(?:[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]|[\u{FE0F}\u{200D}]|[\u{E0020}-\u{E007F}])+/gu

const FENCED_CODE_RE = /```[\s\S]*?(?:```|$)/g
const INLINE_CODE_RE = /`([^`]+)`/g
const MARKDOWN_LINK_RE = /\[([^\]]+)\]\(([^)]+)\)/g
const PARAGRAPH_BREAK_RE = /[ \t]*\n{2,}[ \t]*/g
const SOFT_BREAK_RE = /[ \t]*\n[ \t]*/g

const THINKING_PREFIX_RE =
  /^\s*(?:\([^)\n]{1,48}\)\s*)?(?:processing|thinking|reasoning|analyzing|pondering|contemplating|musing|cogitating|ruminating|deliberating|mulling|reflecting|computing|synthesizing|formulating|brainstorming)\.\.\.\s*/i

const URL_RE = /\bhttps?:\/\/\S+/gi

/** HTML comment form: <!-- speak -->…<!-- /speak --> (case-insensitive, flexible whitespace). */
const SPEAK_COMMENT_RE = /<!--\s*speak\s*-->([\s\S]*?)<!--\s*\/\s*speak\s*-->/gi

/** Fenced form: ```speak … ``` (language tag case-insensitive). */
const SPEAK_FENCE_RE = /^```speak[ \t]*\r?\n([\s\S]*?)^```[ \t]*$/gim

function normalizeLineBreaks(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .replace(/(\p{L})-\n(\p{L})/gu, '$1$2')
    .replace(PARAGRAPH_BREAK_RE, '. ')
    .replace(SOFT_BREAK_RE, ' ')
}

/**
 * If the message contains one or more speak-only blocks, return their inner
 * text joined for TTS. Returns null when none are present, or when every block
 * is empty/whitespace (caller should fall through to full-message sanitize).
 */
export function extractSpeakBlocks(text: string): string | null {
  type Hit = { index: number; body: string }
  const hits: Hit[] = []

  for (const match of text.matchAll(SPEAK_COMMENT_RE)) {
    if (match.index !== undefined) {
      hits.push({ index: match.index, body: match[1] ?? '' })
    }
  }

  for (const match of text.matchAll(SPEAK_FENCE_RE)) {
    if (match.index !== undefined) {
      hits.push({ index: match.index, body: match[1] ?? '' })
    }
  }

  if (hits.length === 0) {
    return null
  }

  hits.sort((a, b) => a.index - b.index)
  const parts = hits.map(h => h.body.trim()).filter(Boolean)

  if (parts.length === 0) {
    return null
  }

  // Join with a sentence boundary only when the previous part lacks terminal punct.
  let joined = parts[0]
  for (let i = 1; i < parts.length; i++) {
    const prev = joined.trimEnd()
    const sep = /[.!?…]$/.test(prev) ? ' ' : '. '
    joined = prev + sep + parts[i]
  }
  return joined
}

function sanitizeCore(text: string): string {
  return normalizeLineBreaks(text)
    .replace(FENCED_CODE_RE, ' ')
    .replace(THINKING_PREFIX_RE, ' ')
    .replace(MARKDOWN_LINK_RE, '$1')
    .replace(INLINE_CODE_RE, '$1')
    .replace(URL_RE, ' link ')
    .replace(EMOJI_RE, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/[*_~>#]/g, '')
    .replace(/^\s*[-+*]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function stripEmptySpeakMarkers(text: string): string {
  return text.replace(SPEAK_COMMENT_RE, '').replace(SPEAK_FENCE_RE, '')
}

/**
 * Prepare assistant text for TTS / read-aloud.
 * When <!-- speak -->…<!-- /speak --> or ```speak fences are present, only
 * those blocks are spoken (dual-document: detailed body + short recap).
 */
export function sanitizeTextForSpeech(text: string): string {
  const speakOnly = extractSpeakBlocks(text)
  if (speakOnly !== null) {
    return sanitizeCore(speakOnly)
  }
  return sanitizeCore(stripEmptySpeakMarkers(text))
}
