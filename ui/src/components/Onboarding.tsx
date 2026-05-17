/**
 * First-launch tour. Three slides that introduce what Vibechek is, what it
 * does, and the safety contract around tag writes. Persisted via
 * `config.ui.seen_onboarding` — the debounced auto-save flips it to true the
 * moment the user dismisses, so the overlay never re-appears.
 *
 * Rendered conditionally from App.tsx; it sits above everything else as a
 * full-screen modal overlay.
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Music, FolderTree, Sparkles, Shield, ArrowRight, Disc3 } from "lucide-react";
import { clsx as cx } from "clsx";

import { useConfigStore } from "../stores";

interface Slide {
  title: string;
  body: React.ReactNode;
}

export function Onboarding() {
  const updateUI = useConfigStore((s) => s.updateUI);
  const [step, setStep] = useState(0);

  const slides: Slide[] = [
    {
      title: "Welcome to Vibechek",
      body: (
        <div className="space-y-4">
          <div className="w-16 h-16 mx-auto rounded-2xl bg-accent/20 flex items-center justify-center">
            <Disc3 className="w-8 h-8 text-accent" />
          </div>
          <p className="text-white/80 leading-relaxed">
            Vibechek is an ML-powered DJ library organizer. Open a folder of music,
            analyze it, then tag, dedupe, or organize.
          </p>
          <p className="text-sm text-white/50">
            Your library stays on your machine — nothing is uploaded.
          </p>
        </div>
      ),
    },
    {
      title: "What it does",
      body: (
        <div className="space-y-4 text-left">
          <FeatureRow
            icon={<Music className="w-5 h-5" />}
            color="text-accent"
            title="Analyze"
            text="ML detects genre, mood, energy, BPM, and key."
          />
          <FeatureRow
            icon={<FolderTree className="w-5 h-5" />}
            color="text-accent-cyan"
            title="Organize"
            text="Sort into clean genre / subgenre folders."
          />
          <FeatureRow
            icon={<Sparkles className="w-5 h-5" />}
            color="text-accent-green"
            title="Dedupe"
            text="Catches re-encoded copies, not just exact matches."
          />
        </div>
      ),
    },
    {
      title: "Safety first",
      body: (
        <div className="space-y-4">
          <div className="w-16 h-16 mx-auto rounded-2xl bg-accent-yellow/20 flex items-center justify-center">
            <Shield className="w-8 h-8 text-accent-yellow" />
          </div>
          <p className="text-white/80 leading-relaxed">
            Vibechek never touches your files until you confirm.
          </p>
          <p className="text-sm text-white/60 leading-relaxed">
            Tags backup is one click in the Tags tab. Built-in undo isn&apos;t here yet —
            back up tags before any operation that writes.
          </p>
        </div>
      ),
    },
  ];

  const isLast = step === slides.length - 1;
  const current = slides[step];

  const finish = () => {
    updateUI({ seen_onboarding: true });
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[80] bg-black/70 backdrop-blur-sm flex items-center justify-center px-4"
    >
      <motion.div
        initial={{ scale: 0.96, y: 12 }}
        animate={{ scale: 1, y: 0 }}
        className="panel max-w-md w-full"
      >
        <div className="px-6 pt-6 pb-2">
          <div className="text-[10px] uppercase tracking-wider text-white/40 mb-1">
            {step + 1} of {slides.length}
          </div>
          <h2 className="font-display font-semibold text-xl text-white">
            {current.title}
          </h2>
        </div>

        <div className="px-6 py-4 min-h-[200px] text-center">
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              transition={{ duration: 0.15 }}
            >
              {current.body}
            </motion.div>
          </AnimatePresence>
        </div>

        {/* Progress dots */}
        <div className="flex items-center justify-center gap-1.5 py-2">
          {slides.map((_, i) => (
            <span
              key={i}
              className={cx(
                "h-1.5 rounded-full transition-all",
                i === step ? "w-6 bg-accent" : "w-1.5 bg-white/15",
              )}
            />
          ))}
        </div>

        <div className="flex items-center justify-between gap-2 px-5 py-3 border-t border-white/5">
          <button className="btn-ghost text-xs" onClick={finish}>
            Skip
          </button>
          {isLast ? (
            <button className="btn-primary" onClick={finish} autoFocus>
              <Sparkles className="w-4 h-4" />
              Start using Vibechek
            </button>
          ) : (
            <button
              className="btn-primary"
              onClick={() => setStep((s) => s + 1)}
              autoFocus
            >
              Next
              <ArrowRight className="w-4 h-4" />
            </button>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}

interface FeatureRowProps {
  icon: React.ReactNode;
  color: string;
  title: string;
  text: string;
}

function FeatureRow({ icon, color, title, text }: FeatureRowProps) {
  return (
    <div className="flex items-start gap-3 px-2 py-2">
      <div className={cx("flex-none mt-0.5", color)}>{icon}</div>
      <div className="flex-1">
        <div className="text-sm font-medium text-white">{title}</div>
        <div className="text-xs text-white/60">{text}</div>
      </div>
    </div>
  );
}
