import {
  ArrowDown,
  ArrowUpRight,
  Clock3,
  Code2,
  Github,
  Layers3,
  Monitor,
  Terminal,
} from 'lucide-react';
import logo from './assets/logo.jpeg';
import introVideo from './assets/intro.mp4';
import videoPoster from './assets/video-poster.jpg';

const repository = 'https://github.com/cr-sbarbouche/agvisor';
const linkedinPost =
  'https://www.linkedin.com/feed/update/urn:li:activity:7524033443815641088/';

function Home() {
  return (
    <main className="site-shell">
      <div className="container">
        <header className="topbar" aria-label="Main navigation">
          <a className="brand" href="#top" aria-label="Agvisor home" data-testid="link-home">
            <img src={logo} alt="" />
            <span className="brand-word">agvisor</span>
          </a>
          <nav className="nav-links" aria-label="Product">
            <a href="#features" data-testid="link-features">What it does</a>
            <a href="#quickstart" data-testid="link-quickstart">Quickstart</a>
            <a className="nav-cta" href={repository} target="_blank" rel="noreferrer" data-testid="link-github-nav">
              <Github size={15} aria-hidden="true" /> GitHub <ArrowUpRight size={13} aria-hidden="true" />
            </a>
          </nav>
        </header>

        <section className="hero" id="top" aria-labelledby="hero-title">
          <div>
            <div className="eyebrow reveal"><span className="eyebrow-dot" /> Python · Docker · gVisor</div>
            <h1 className="display reveal delay-1" id="hero-title">Run agent code in<br /><span>bounded sandboxes.</span></h1>
            <p className="hero-copy reveal delay-2">
              Agvisor is a small Python library for creating and managing Docker-backed sandboxes for coding-agent workflows, using gVisor’s runsc runtime.
            </p>
            <div className="hero-actions reveal delay-2">
              <a className="button button-primary" href={repository} target="_blank" rel="noreferrer" data-testid="link-github-hero">
                <Github size={16} aria-hidden="true" /> Explore the repository <ArrowUpRight size={15} aria-hidden="true" />
              </a>
              <a className="button button-secondary" href="#quickstart" data-testid="link-install-hero">
                <Terminal size={15} aria-hidden="true" /> Get started
              </a>
            </div>
            <div className="install-line">Install from PyPI <code>pip install agvisor</code></div>
            <div className="downloads-line" aria-label="Total PyPI downloads">
              <span>PyPI downloads</span>
              <a
                className="downloads-badge"
                href="https://pepy.tech/projects/agvisor"
                target="_blank"
                rel="noreferrer"
                data-testid="link-pypi-downloads"
              >
                <img
                  src="https://static.pepy.tech/personalized-badge/agvisor?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads"
                  alt="Agvisor total downloads on PyPI"
                />
              </a>
            </div>
          </div>
          <div className="hero-visual" aria-label="Agvisor logo and sandbox details">
            <div className="visual-orbit" aria-hidden="true" />
            <div className="floating-note note-a"><strong>runtime</strong>runsc · gVisor</div>
            <div className="floating-note note-b"><strong>execution</strong>Docker-backed</div>
            <img className="hero-logo" src={logo} alt="Agvisor — Sandboxes for Coding Agents" />
          </div>
        </section>

        <section className="section section-tint" id="features" aria-labelledby="features-title">
          <div className="section-head">
            <div>
              <div className="section-kicker">The small practical layer</div>
              <h2 id="features-title">Control the run.<br />Keep your workflow.</h2>
            </div>
            <p className="section-intro">
              Agvisor wraps the Docker CLI with gVisor, then gives your Python code a straightforward way to create, inspect, reuse and stop sandboxes. You choose the limits and lifecycle that fit your workflow.
            </p>
          </div>
          <div className="feature-layout">
            <article className="feature-panel feature-wide">
              <span className="feature-index">01 / RESOURCE BOUNDS</span>
              <h3>Set boundaries before code runs.</h3>
              <p>Configure practical resource limits on a sandbox instead of treating generated code like an ordinary local process.</p>
              <div className="limit-list" aria-label="Available resource limits">
                <span>CPU</span><span>Memory</span><span>Processes</span><span>Shared memory</span><span>Disk</span>
              </div>
            </article>
            <article className="feature-panel">
              <div className="feature-icon"><Layers3 size={20} aria-hidden="true" /></div>
              <h3>Named and resumable</h3>
              <p>Give sandboxes names and resume them across steps. List, stop, delete, or execute commands inside them.</p>
            </article>
            <article className="feature-panel">
              <div className="feature-icon"><Clock3 size={20} aria-hidden="true" /></div>
              <h3>Lifecycle controls</h3>
              <p>Set a maximum lifetime and idle expiry so sandboxes can be cleaned up as your task flow moves on.</p>
            </article>
          </div>
        </section>

        <section className="section" aria-labelledby="workflow-title">
          <div className="section-head">
            <div>
              <div className="section-kicker">A flexible foundation</div>
              <h2 id="workflow-title">Made to fit around your agent.</h2>
            </div>
            <p className="section-intro">Use the library as one component in your own coding-agent loop. Agvisor handles sandbox operations; your application decides what to run and how to review the result.</p>
          </div>
          <div className="workflow">
            <article className="step">
              <span className="step-number">01 — CONFIGURE</span>
              <h3>Choose the host</h3>
              <p>Use a local Docker host or point Agvisor at a remote Docker host over SSH.</p>
            </article>
            <article className="step">
              <span className="step-number">02 — RUN</span>
              <h3>Bound the execution</h3>
              <p>Create a sandbox with the resource and lifetime settings your workflow requires.</p>
            </article>
            <article className="step">
              <span className="step-number">03 — MANAGE</span>
              <h3>Continue or clean up</h3>
              <p>Run commands, resume named sandboxes, then stop or delete when the work is done.</p>
            </article>
          </div>
        </section>

        <section className="section video-section" aria-labelledby="video-title">
          <div className="section-head">
            <div>
              <div className="section-kicker">A short introduction</div>
              <h2 id="video-title">Why agent code needs a sandbox.</h2>
            </div>
            <p className="section-intro">
              A quick overview of the problem Agvisor is built for and where a resource-bounded sandbox fits in a coding-agent workflow.
            </p>
          </div>
          <div className="video-frame">
            <video
              controls
              playsInline
              preload="metadata"
              poster={videoPoster}
              aria-label="Agvisor introduction video from LinkedIn"
              data-testid="video-agvisor-intro"
            >
              <source src={introVideo} type="video/mp4" />
              Your browser does not support the video element.
            </video>
          </div>
          <div className="video-caption">
            <span>Agvisor introduction · 27 seconds</span>
            <a href={linkedinPost} target="_blank" rel="noreferrer" data-testid="link-video-linkedin-post">
              View the LinkedIn post <ArrowUpRight size={13} aria-hidden="true" />
            </a>
          </div>
        </section>

        <section className="section section-tint" id="quickstart" aria-labelledby="quickstart-title">
          <div className="code-section">
            <div>
              <div className="section-kicker">Quick start</div>
              <h2 id="quickstart-title">Start with the package.</h2>
              <p>Install Agvisor from PyPI, then use the project’s README for current setup instructions and API details.</p>
              <a className="button button-primary" href={`${repository}#readme`} target="_blank" rel="noreferrer" data-testid="link-readme">
                Read the README <ArrowUpRight size={15} aria-hidden="true" />
              </a>
            </div>
            <div className="code-window" role="region" aria-label="Installation command">
              <div className="code-bar"><i /><i /><i /><span>terminal</span></div>
              <pre><span className="code-muted">$</span> <span className="code-gold">pip install agvisor</span>{'\n'}<span className="code-muted"># Python library · Docker CLI · gVisor runsc</span></pre>
            </div>
          </div>
          <div className="feature-layout" style={{ marginTop: 34 }}>
            <article className="feature-panel">
              <div className="feature-icon"><Monitor size={20} aria-hidden="true" /></div>
              <h3>Optional desktop over RDP</h3>
              <p>For workflows that need a graphical session, Agvisor can provide optional GNOME desktop access over RDP.</p>
            </article>
            <article className="feature-panel">
              <div className="feature-icon"><Code2 size={20} aria-hidden="true" /></div>
              <h3>Related: agvisor-desktop</h3>
              <p>A separate npm package provides an embeddable web component for the desktop experience. It is not required for the Python library.</p>
              <a href="https://www.npmjs.com/package/agvisor-desktop" target="_blank" rel="noreferrer" style={{ color: '#29284f', fontSize: 12, fontWeight: 700, display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 15 }} data-testid="link-desktop-package">
                View the npm package <ArrowUpRight size={13} aria-hidden="true" />
              </a>
            </article>
          </div>
        </section>

        <section className="section" aria-labelledby="security-title">
          <div className="section-head">
            <div>
              <div className="section-kicker">Read before use</div>
              <h2 id="security-title">Alpha software. Review your threat model.</h2>
            </div>
            <p className="section-intro">gVisor is one layer in a broader isolation setup. It does not make arbitrary code fully secure, and it cannot replace application-level controls or careful host configuration.</p>
          </div>
          <aside className="warning" aria-label="Security status">
            <div className="warning-mark" aria-hidden="true">!</div>
            <div>
              <h3>Agvisor is alpha and has not been security-audited.</h3>
              <p>Review the README’s security notes before use. In particular, consider cloud metadata access and network exposure in your environment. Treat sandboxing as defense in depth, not a guarantee against hostile code.</p>
            </div>
          </aside>
        </section>

        <section className="bottom-cta" aria-labelledby="cta-title">
          <div className="eyebrow" style={{ justifyContent: 'center', marginBottom: 18 }}><span className="eyebrow-dot" /> Open source · Python</div>
          <h2 id="cta-title">Build the workflow<br />you need around it.</h2>
          <p>Read the source, review the caveats, and install when you’re ready.</p>
          <div className="hero-actions" style={{ justifyContent: 'center' }}>
            <a className="button button-primary" href={repository} target="_blank" rel="noreferrer" data-testid="link-github-footer">
              <Github size={16} aria-hidden="true" /> View Agvisor on GitHub <ArrowUpRight size={14} aria-hidden="true" />
            </a>
            <a className="button button-secondary" href="#top" data-testid="link-back-to-top">
              <ArrowDown size={14} style={{ transform: 'rotate(180deg)' }} aria-hidden="true" /> Back to top
            </a>
          </div>
        </section>

        <footer className="footer">
          <div className="footer-note">© Agvisor · A practical tool for coding-agent workflows.</div>
          <nav className="footer-links" aria-label="More links">
            <a href={repository} target="_blank" rel="noreferrer" data-testid="link-footer-repository">GitHub</a>
            <a href={`${repository}#readme`} target="_blank" rel="noreferrer" data-testid="link-footer-docs">README &amp; docs</a>
            <a href={linkedinPost} target="_blank" rel="noreferrer" data-testid="link-linkedin">LinkedIn</a>
          </nav>
        </footer>
      </div>
    </main>
  );
}

function App() {
  return <Home />;
}

export default App;
