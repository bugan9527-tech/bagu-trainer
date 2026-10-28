import { useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, Copy } from 'lucide-react';
import { request } from './state';

function CodeBlock({ children }: { children?: ReactNode }) {
  const content = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  return <div className="code-block"><button className="copy-code" aria-label="复制代码" onClick={() => {
    request('native.copyText', { text: content.current?.textContent || '' }).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1800);
    }).catch(() => setError('复制未完成，请手动选择代码。'));
  }}>{copied ? <Check size={15} /> : <Copy size={15} />}{copied ? '已复制' : '复制'}</button>
    <pre ref={content}>{children}</pre>{error && <small role="status">{error}</small>}</div>;
}
export function Markdown({ text }: { text?: string }) {
  const [error, setError] = useState('');
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} skipHtml
    components={{ pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
      img: ({ alt }) => <span className="muted">{alt ? `[图片：${alt}]` : '[图片]'}</span>,
      a: ({ href, children }) => <button className="text-link" onClick={() => {
        request('native.openExternal', { url: href }).catch((error) => setError(error.message));
      }}>{children}</button>,
    }}>{text || ''}</ReactMarkdown>{error && <p className="notice error" role="alert">{error}</p>}</div>;
}
