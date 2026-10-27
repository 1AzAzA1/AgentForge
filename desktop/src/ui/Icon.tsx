// Иконки Lucide (ISC) по именам, которые приходят и из Python (роли,
// форматы, причины вопросов человеку), и из кода страниц.

import {
  Activity, ArrowDown, ArrowRight, ArrowUpRight, BadgeCheck, Bell, BookOpen, Bot, Box, Brain,
  ChartBar, ChartLine, Check, ChevronDown, ChevronUp, CircleAlert, CircleCheck, CircleDot,
  CirclePause, CirclePlay, CircleX, Coins, Command, Copy, Cpu, Database, DollarSign, Download,
  Eraser, ExternalLink, Eye, EyeOff, FileArchive, FileText, FileType, Flag, Folder, FolderOpen,
  Gauge, GitBranch, Globe, Hand, HardDrive, Inbox, Info, KeyRound, Layers, LayoutDashboard,
  ListChecks, Lock, LogOut, MessageSquareText, Monitor, OctagonX, Package, Pause, Pencil, Play,
  Plus, RefreshCw, RotateCcw, Scale, ScanEye, ScrollText, Search, Settings, ShieldCheck,
  SkipForward, Sparkles, Split, Square, SquarePen, Star, Terminal, Trash2, TrendingUp,
  TriangleAlert, User, Users, Wallet, WandSparkles, Workflow, X, Zap,
  type LucideIcon,
} from "lucide-react";
import type { CSSProperties } from "react";

const ICONS: Record<string, LucideIcon> = {
  activity: Activity, "arrow-down": ArrowDown, "arrow-right": ArrowRight,
  "arrow-up-right": ArrowUpRight, "badge-check": BadgeCheck, bell: Bell, "book-open": BookOpen,
  bot: Bot, box: Box, brain: Brain, "chart-bar": ChartBar, "chart-line": ChartLine, check: Check,
  "chevron-down": ChevronDown, "chevron-up": ChevronUp, "circle-alert": CircleAlert,
  "circle-check": CircleCheck, "circle-dot": CircleDot, "circle-pause": CirclePause,
  "circle-play": CirclePlay, "circle-x": CircleX, coins: Coins, command: Command, copy: Copy,
  cpu: Cpu, database: Database, "dollar-sign": DollarSign, download: Download, eraser: Eraser,
  "external-link": ExternalLink, eye: Eye, "eye-off": EyeOff, "file-archive": FileArchive,
  "file-text": FileText, "file-type": FileType, flag: Flag, folder: Folder,
  "folder-open": FolderOpen, gauge: Gauge, "git-branch": GitBranch, globe: Globe, hand: Hand,
  "hard-drive": HardDrive, inbox: Inbox, info: Info, "key-round": KeyRound, layers: Layers,
  "layout-dashboard": LayoutDashboard, "list-checks": ListChecks, lock: Lock, "log-out": LogOut,
  "message-square-text": MessageSquareText, monitor: Monitor, "octagon-x": OctagonX,
  package: Package, pause: Pause, pencil: Pencil, play: Play, plus: Plus, "refresh-cw": RefreshCw,
  "rotate-ccw": RotateCcw, scale: Scale, "scan-eye": ScanEye, "scroll-text": ScrollText,
  search: Search, settings: Settings, "shield-check": ShieldCheck, "skip-forward": SkipForward,
  sparkles: Sparkles, split: Split, square: Square, "square-pen": SquarePen, star: Star,
  terminal: Terminal, "trash-2": Trash2, "trending-up": TrendingUp,
  "triangle-alert": TriangleAlert, user: User, users: Users, wallet: Wallet,
  "wand-sparkles": WandSparkles, workflow: Workflow, x: X, zap: Zap,
};

interface Props {
  name: string;
  size?: number;
  color?: string;
  className?: string;
  style?: CSSProperties;
  strokeWidth?: number;
}

export function Icon({ name, size = 16, color, className, style, strokeWidth = 2 }: Props) {
  const Glyph = ICONS[name] ?? CircleDot;
  return (
    <Glyph
      size={size}
      color={color ?? "currentColor"}
      strokeWidth={strokeWidth}
      className={className}
      style={{ flex: "none", ...style }}
      aria-hidden
    />
  );
}
