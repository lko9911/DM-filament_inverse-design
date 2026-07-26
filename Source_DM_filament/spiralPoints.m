function Sref = spiralPoints(ds, p, a, numPoints, move_origin)
% ds = required point distance along the arc path
% p = increment per rev, pitch
% a = inner radius
% move_origin = move origin from (0,0) to center of the bed [mx my]
% numPoints = total length / ds

    Sref = zeros(numPoints,4);

    r = a;
    b = p / (2*pi);
    phi = r / b;   
    
    for i = 1:numPoints
        phi = phi + (ds / r);
        r = b * phi;
        x = r*cos(phi)+ move_origin(1);
        y = r*sin(phi)+ move_origin(2);
        Sref(i,:) = [x, y, r, phi];
    end
    Sref(1:3,:) = [];      
end

% function [sx, sy] = spiralPoints(arc, separation, numpoints)
% % arc = required point distance along the path
% % seperation = required separation of the spiral arms.
% 
%     %polar to cartesian
%     function [ rx,ry ] = p2c(rr, phi)
%         rx = rr * cos(phi);
%         ry = rr * sin(phi);
%     end
% 
%     sx = zeros(numpoints,1);
%     sy = zeros(numpoints,1);
% 
%     r = arc;
%     b = separation / (2 * pi());
%     phi = r / b;
% 
%     while numpoints > 0
%         [ sx(numpoints), sy(numpoints) ] = p2c(r, phi);
%         phi = phi + (arc / r);
%         r = b * phi;
%         numpoints = numpoints - 1;
%     end
% 
% end