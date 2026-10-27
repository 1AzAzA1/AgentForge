// Шаблон страницы: заголовок с действиями и прокручиваемое содержимое.
// fill - содержимое растягивается на всю высоту без общей прокрутки
// (страницы со своими прокручиваемыми панелями, как "Выполнение").

import type { ReactNode } from "react";
import { Icon } from "./Icon";

interface PageProps {
  title: string;
  subtitle?: string;
  icon?: string;
  actions?: ReactNode;
  fill?: boolean;
  children?: ReactNode;
}

export function Page({ title, subtitle = "", icon = "", actions, fill = false, children }: PageProps) {
  return (
    <div className="page">
      <header className="page-head">
        {icon && (
          <div className="page-icon">
            <Icon name={icon} size={20} color="var(--primary-soft)" />
          </div>
        )}
        <div className="grow">
          <h1 className="h1 ellipsis">{title}</h1>
          {subtitle && <div className="dim clamp2">{subtitle}</div>}
        </div>
        {actions && <div className="row page-actions">{actions}</div>}
      </header>
      <div className={fill ? "page-body is-fill" : "page-body"}>{children}</div>
    </div>
  );
}
