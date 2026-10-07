import './globals.css';
import MockBar from '../src/components/MockBar';

export const metadata = {
  title: 'Edenn Console',
  description: 'Balance, usage, and API key management',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        {children}
        {/* Renders null unless the build set NEXT_PUBLIC_MOCK=1. It lives in
            the layout rather than on the console page so it is also present on
            the sign-in gate and the docs tree — every screen showing invented
            data says so. */}
        <MockBar />
      </body>
    </html>
  );
}
