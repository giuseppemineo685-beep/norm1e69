import { login } from "../actions";

export default async function Login({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { error } = await searchParams;
  return (
    <main className="wrap login">
      <h1>Job hunt</h1>
      <p className="muted">Introduce la contrasena para ver las ofertas.</p>
      <form action={login}>
        <input type="password" name="password" placeholder="Contrasena" autoFocus required />
        {error && <p style={{ color: "var(--bad)" }}>Contrasena incorrecta.</p>}
        <button className="primary" type="submit">Entrar</button>
      </form>
    </main>
  );
}
