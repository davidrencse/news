/* Math and code rendering for articles, shared by the in-app reader and the PDF printer. */
(function () {
  const TEX_HINT = /[\\^_{}]|\b(?:frac|sqrt|sum|int|prod|lim|alpha|beta|gamma|delta|theta|lambda|sigma|mu|pi|infty)\b/;

  // "$…$" counts as math only when its contents look like TeX, so "$5 and $10" stays plain text.
  function markInlineDollars(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.parentElement.closest('pre, code, .katex, script, style, textarea')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) if (walker.currentNode.nodeValue.includes('$')) nodes.push(walker.currentNode);
    for (const n of nodes) {
      n.nodeValue = n.nodeValue.replace(/(^|[^\\$])\$(?!\$)([^$\n]{1,400}?)\$(?![\d$])/g, (m, pre, tex) =>
        (TEX_HINT.test(tex) && !/^\s|\s$/.test(tex) ? `${pre}\\(${tex}\\)` : m));
    }
  }

  function renderMath(root) {
    if (!window.renderMathInElement) return;
    markInlineDollars(root);
    window.renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
      throwOnError: false,
    });
  }

  function highlightCode(root) {
    if (!window.hljs) return;
    root.querySelectorAll('pre > code').forEach(code => {
      if (code.dataset.highlighted) return;
      const lang = (code.parentElement.dataset.lang || '').toLowerCase();
      if (lang && window.hljs.getLanguage(lang)) code.classList.add(`language-${lang}`);
      window.hljs.highlightElement(code);
      const detected = /language-([\w+#-]+)/.exec(code.className);
      if (!code.parentElement.dataset.lang && detected) code.parentElement.dataset.lang = detected[1];
    });
  }

  function whenLoaded(root) {
    const pending = [...root.querySelectorAll('img')].filter(img => !img.complete).map(img => new Promise(done => {
      img.addEventListener('load', done, { once: true });
      img.addEventListener('error', done, { once: true });
    }));
    const fonts = document.fonts ? document.fonts.ready : Promise.resolve();
    return Promise.race([Promise.all([Promise.all(pending), fonts]), new Promise(done => setTimeout(done, 25000))]);
  }

  window.ArticleDoc = {
    /** Render math and code in place. Deterministic, so highlight offsets stay valid across visits. */
    render(root) {
      if (root.dataset.rendered) return;
      root.dataset.rendered = '1';
      window.hljs?.configure({ ignoreUnescapedHTML: true });
      try { renderMath(root); } catch (e) { console.warn('math', e); }
      try { highlightCode(root); } catch (e) { console.warn('code', e); }
    },
    /** Resolves once images and fonts have loaded (used before printing). */
    ready: whenLoaded,
  };
})();
