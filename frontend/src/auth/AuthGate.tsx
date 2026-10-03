import { type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "./AuthContext";

interface GateProps {
  title: string;
  hint: string;
  children: ReactNode;
}

/** Renders `children` for signed-in users, otherwise the centred «доступно только авторизованным» panel. */
export function RequireAuth({ title, hint, children }: GateProps) {
  const { user } = useAuth();
  if (user) return <>{children}</>;
  return (
    <div className="card anim-card px-5 py-14 text-center">
      <div className="mb-2 text-[17px] font-extrabold">{title}</div>
      <p className="mx-auto mb-5 max-w-md text-sm leading-relaxed text-ink/55">{hint}</p>
      <Link className="btn btn-primary" to="/auth/login">
        Войти
      </Link>
    </div>
  );
}
