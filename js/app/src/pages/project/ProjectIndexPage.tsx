import { Navigate, useLocation } from "react-router";

export const ProjectIndexPage = () => {
  const location = useLocation();
  return (
    <Navigate
      to={{
        pathname: "sessions",
        search: location.search,
        hash: location.hash,
      }}
      replace
    />
  );
};
